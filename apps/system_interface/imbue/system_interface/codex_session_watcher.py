"""Tail a codex agent's live rollout and emit UI events.

The codex analogue of :class:`claude_session_watcher.ClaudeSessionWatcher`. It tails
codex's OWN on-disk rollout in real time -- the same file codex writes as it works --
parses each line to the UI event schema via
:func:`codex_session_parser.parse_codex_rollout_line`, dedups by ``event_id``, and
fans new events out through ``on_events`` (the same callback contract
``ClaudeSessionWatcher`` uses, so :mod:`app_context`'s broadcast/SSE plumbing is
unchanged). It reads the live file -- not mngr_codex's stream_transcript.sh mirror --
because the mirror lags codex by up to its 1s poll, long enough for the optimistic
"sending" bubble to visibly flip to "queued" before it reconciles. Reading the live
file directly is how the claude watcher already works.

Which rollout is live rotates (a fresh file per session, and again on resume), so --
like claude following its ``claude_session_id_history`` -- we follow the active file
via a marker: mngr_codex writes its absolute path to
``<agent_state_dir>/codex_transcript_path`` every turn. Each cycle we re-read that
marker; when it points somewhere new we switch files (from the new file's start),
keeping the global line counter, tool-name map, and accumulated events/dedup so a
resume's re-serialised history (same codex ``id``s) dedups against what we already
emitted. The watchdog is a recursive observer on the stable
``<agent_state_dir>/plugin/codex/home/sessions`` dir (all rollouts live under it), so
appends -- to whichever rollout is live -- wake the loop immediately, with the 1s poll
as a safety net.

Simpler than the claude watcher in the parse layer: no two-tier cache (the parser
reads incrementally in order and never reparses a single line, so a plain in-memory
list + stable event ids suffice), and (this first cut) no subagent-session tracking.
It exposes the same read/pagination API the server calls; ``session_id`` on those
methods is inert (codex is one logical session to the UI) and
:meth:`get_subagent_metadata` always returns ``None``.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any
from typing import Callable

from loguru import logger as _loguru_logger
from watchdog.observers import Observer

from imbue.system_interface.codex_session_parser import parse_codex_rollout_line
from imbue.system_interface.watcher_common import POLL_INTERVAL_SECONDS
from imbue.system_interface.watcher_common import WakeOnChangeHandler

logger = _loguru_logger

# We tail codex's LIVE rollout directly (the same real-time file codex writes),
# not the stream_transcript.sh mirror -- the mirror lags codex by up to its 1s poll,
# long enough for the "sending" bubble to visibly flip to "queued" before it
# reconciles. Reading the live file is the codex analogue of how the claude watcher
# reads claude's own on-disk transcript directly.
#
# Which file is live rotates (a new rollout per session, and again on resume), so
# like claude (claude_session_id_history), we follow it via a marker: mngr_codex
# writes the active rollout's absolute path to <agent_state_dir>/codex_transcript_path
# on every turn. All rollouts live under <agent_state_dir>/plugin/codex/home/sessions,
# which we watchdog recursively (stable path, catches every rollout's appends without
# re-scheduling on rotation). Constants kept local (not imported from the plugin),
# mirroring claude_session_parser's reimplement-don't-import stance.
_MARKER_RELATIVE = Path("codex_transcript_path")
_SESSIONS_RELATIVE = Path("plugin") / "codex" / "home" / "sessions"


class CodexSessionWatcher:
    """Watches a codex agent's raw rollout file and emits parsed UI events."""

    def __init__(
        self,
        agent_id: str,
        agent_state_dir: Path,
        on_events: Callable[[str, list[dict[str, Any]]], None],
    ) -> None:
        self._agent_id = agent_id
        # Marker file holding the active rollout's absolute path (rewritten each turn,
        # so it follows rotation), and the sessions dir we watchdog.
        self._marker_path = agent_state_dir / _MARKER_RELATIVE
        self._sessions_dir = agent_state_dir / _SESSIONS_RELATIVE
        self._on_events = on_events

        # Guards the in-memory transcript mirror and the tail cursor. Held across
        # the (cheap, incremental) file read + adapt, but never across the
        # ``on_events`` fan-out callback -- the same discipline ClaudeSessionWatcher
        # follows.
        self._lock = threading.Lock()
        # Adapted UI events, in append (chronological) order.
        self._events: list[dict[str, Any]] = []
        # event_id -> index into _events, for O(1) offset lookup + dedup.
        self._event_index: dict[str, int] = {}
        # The rollout file currently being tailed (resolved from the marker); None
        # until the first turn writes the marker. Rotation = marker points elsewhere.
        self._current_path: Path | None = None
        # Bytes of _current_path already consumed; reset only on rotation / re-read.
        self._byte_offset = 0
        # A trailing partial line (no newline yet) carried to the next read.
        self._partial = ""
        # GLOBAL monotonic line counter for synthetic event ids (event_msg user_message
        # has no codex id). Never reset -- keeps ids unique ACROSS rollout files so a
        # resume's line 5 can't collide with the prior file's line 5. (id-based events
        # use codex's own msg id / call_id, so they dedup re-serialised copies
        # regardless.)
        self._line_index = 0
        # call_id -> tool_name, so a function_call_output can recover its tool name
        # from the earlier function_call. Persists across files (a resume re-serialises
        # the calls, but keeping the map is harmless and covers output-only cases).
        self._tool_name_by_call_id: dict[str, str] = {}

        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        # Watchdog observer on the transcript dir, so an append wakes the loop
        # immediately instead of waiting out the poll interval. Started lazily once
        # the dir exists (see _maybe_start_observer).
        self._observer: Any = None

    def start(self) -> None:
        """Start tailing the transcript in a background thread."""
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"codex-watcher-{self._agent_id}")
        self._thread.start()

    def stop(self) -> None:
        """Stop tailing."""
        self._stop_event.set()
        self._wake_event.set()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5.0)
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    # --- background loop ---------------------------------------------------

    def _maybe_start_observer(self) -> None:
        """Watch the transcript dir so an append wakes the loop immediately.

        No-op if already watching or the dir does not exist yet -- the transcript
        dir is created by mngr_codex's stream_transcript.sh on the agent's first
        turn, so until then the poll interval covers the gap and this is retried
        each loop. Watching the transcript dir (not the whole state dir) keeps us
        off the noisy per-agent sqlite/log writes elsewhere. The 1s poll remains a
        safety net if watchdog misses an event.
        """
        if self._observer is not None:
            return
        if not self._sessions_dir.is_dir():
            return
        try:
            observer = Observer()
            # Recursive: rollouts live under sessions/YYYY/MM/DD/ and rotate across
            # days/sessions, so watching the stable sessions root catches every
            # rollout's appends without re-scheduling on rotation.
            observer.schedule(WakeOnChangeHandler(self._wake_event), str(self._sessions_dir), recursive=True)
            observer.start()
            self._observer = observer
        except OSError as e:
            logger.debug("codex watcher: failed to start watchdog on {}: {}", self._sessions_dir, e)

    def _run(self) -> None:
        # Emit whatever already exists on first read (agent may have run before the
        # UI connected), then poll (woken early by watchdog) for appended lines.
        self._maybe_start_observer()
        self._emit(self._consume_new_lines())
        while not self._stop_event.is_set():
            self._wake_event.wait(timeout=POLL_INTERVAL_SECONDS)
            self._wake_event.clear()
            if self._stop_event.is_set():
                break
            self._maybe_start_observer()  # retry until the transcript dir exists
            self._emit(self._consume_new_lines())

    def _emit(self, events: list[dict[str, Any]]) -> None:
        if events:
            self._on_events(self._agent_id, events)

    def _read_active_rollout(self) -> Path | None:
        """The absolute path of the live rollout, per the marker; None until written."""
        try:
            raw = self._marker_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None  # no marker yet (agent hasn't taken a turn) -- normal
        return Path(raw) if raw else None

    def _consume_new_lines(self) -> list[dict[str, Any]]:
        """Read bytes appended to the live rollout since the last cursor, following
        rotation (a new rollout on resume) via the marker."""
        target = self._read_active_rollout()
        if target is None:
            return []

        new_events: list[dict[str, Any]] = []
        with self._lock:
            if target != self._current_path:
                # First resolution or rotation (resume -> new rollout). Tail the new
                # file from its start. Keep _line_index (global -> ids stay unique
                # across files), _tool_name_by_call_id, and _events/_event_index so a
                # resume's re-serialised history (same codex msg ids) dedups against
                # what we already emitted and the accumulated transcript survives.
                self._current_path = target
                self._byte_offset = 0
                self._partial = ""

            try:
                size = target.stat().st_size
            except OSError:
                return []  # marker points at a not-yet-created file; retry next cycle

            # Codex rollouts are append-only; a shrink is unexpected. Re-read from the
            # start -- id-based dedup drops the re-emitted assistant/tool events.
            if size < self._byte_offset:
                self._byte_offset = 0
                self._partial = ""
            if size == self._byte_offset and not self._partial:
                return []

            try:
                with target.open("rb") as f:
                    f.seek(self._byte_offset)
                    raw = f.read()
            except OSError:
                logger.debug("codex watcher: failed to read {}", target)
                return []
            self._byte_offset += len(raw)

            data = self._partial + raw.decode("utf-8", errors="replace")
            lines = data.split("\n")
            # The final element is the trailing (possibly empty) partial line; carry
            # it forward so a half-written record is completed on the next read.
            self._partial = lines.pop()

            for line in lines:
                # Every physical line consumes an index (even blanks/skips) so a
                # given line always maps to the same id across the run.
                idx = self._line_index
                self._line_index += 1
                stripped = line.strip()
                if not stripped:
                    continue
                for event in self._adapt_line(stripped, idx):
                    event_id = event["event_id"]
                    if event_id in self._event_index:
                        continue
                    self._event_index[event_id] = len(self._events)
                    self._events.append(event)
                    new_events.append(event)

        return new_events

    def _adapt_line(self, line: str, line_index: int) -> list[dict[str, Any]]:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("codex watcher: skipping malformed rollout line")
            return []
        if not isinstance(record, dict):
            return []
        return parse_codex_rollout_line(record, line_index, self._tool_name_by_call_id)

    # --- read API (mirrors AgentSessionWatcher) ----------------------------
    #
    # ``session_id`` is accepted for interface parity with AgentSessionWatcher but
    # is inert: codex's common transcript is a single logical session with no
    # subagent sessions to filter.

    def get_all_events(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """Return every parsed event in chronological order."""
        with self._lock:
            return list(self._events)

    def get_tail_events(self, limit: int, session_id: str | None = None) -> list[dict[str, Any]]:
        """Return the most recent ``limit`` events (chronological order)."""
        if limit <= 0:
            return []
        with self._lock:
            return list(self._events[-limit:])

    def get_backfill_events(
        self, before_event_id: str, limit: int = 50, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Return up to ``limit`` events immediately before ``before_event_id``."""
        if limit <= 0:
            return []
        with self._lock:
            idx = self._event_index.get(before_event_id)
            if idx is None:
                return []
            start = max(0, idx - limit)
            return list(self._events[start:idx])

    def get_forward_events(
        self, after_event_id: str, limit: int = 50, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Return up to ``limit`` events immediately after ``after_event_id``."""
        if limit <= 0:
            return []
        with self._lock:
            idx = self._event_index.get(after_event_id)
            if idx is None:
                return []
            return list(self._events[idx + 1 : idx + 1 + limit])

    def get_events_at_offset(self, offset: int, limit: int, session_id: str | None = None) -> list[dict[str, Any]]:
        """Return up to ``limit`` events starting at global index ``offset`` (clamped)."""
        if limit <= 0:
            return []
        start = max(0, offset)
        with self._lock:
            return list(self._events[start : start + limit])

    def get_event_offset(self, event_id: str, session_id: str | None = None) -> int:
        """Global index of ``event_id``, or -1 if unknown."""
        with self._lock:
            idx = self._event_index.get(event_id)
            return idx if idx is not None else -1

    def get_total_event_count(self, session_id: str | None = None) -> int:
        """Total number of events in the transcript."""
        with self._lock:
            return len(self._events)

    def get_subagent_metadata(self, subagent_session_id: str) -> dict[str, str] | None:
        """codex has no subagent linkage in the common transcript -- always None."""
        return None

    def is_main_session_event(self, event: dict[str, Any]) -> bool:
        """Every codex event belongs to the single main session."""
        return True
