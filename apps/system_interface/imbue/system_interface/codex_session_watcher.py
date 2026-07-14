"""Tail a codex agent's raw rollout and emit UI events.

The codex analogue of :class:`claude_session_watcher.ClaudeSessionWatcher`, but far
simpler. mngr_codex mirrors the live rollout verbatim (no reschematising) to a stable
per-agent path:

    <agent_state_dir>/logs/codex_transcript/events.jsonl

This is the lossless raw codex format (the codex analogue of claude's
``projects/<session>.jsonl``, and richer than the lossy common transcript we
deliberately bypass -- same reason claude reparses its own raw JSONL). This watcher
tails that one append-only file, parses each line to the UI event schema via
:func:`codex_session_parser.parse_codex_rollout_line`, dedups by ``event_id``, and
fans new events out through ``on_events`` -- the same callback contract
``ClaudeSessionWatcher`` uses, so :mod:`app_context`'s broadcast/SSE plumbing is
unchanged.

Simpler than the claude watcher because there is no ``projects/`` tree to walk, no
two-tier cache (the parser reads incrementally in order and never reparses a single
line, so a plain in-memory list + physical-line-index event ids suffice), and (this
first cut) no subagent-session tracking.

It exposes the same read/pagination API the server calls on a watcher, backed by a
simple in-memory ordered list. codex has a single logical session from the UI's
point of view, so the ``session_id`` parameter these methods accept is inert (there
are no subagent sessions to filter) and :meth:`get_subagent_metadata` always returns
``None``.

Watching is a watchdog observer on the transcript dir with the poll loop
(``POLL_INTERVAL_SECONDS``) as a safety net -- the same pattern as the claude
watcher. The observer is started lazily (``_maybe_start_observer``) because the
transcript dir does not exist until the agent's first turn; until then the poll
covers the gap. The watchdog matters for latency: without it, a new message waits
out the poll interval before reaching the UI, which is long enough for the optimistic
"sending" bubble to visibly flip to "queued" before it reconciles.
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

# Relative location of the codex RAW rollout mirror under an agent's state dir.
# mngr_codex's stream_transcript.sh tails the live rollout here verbatim (no
# reschematising), so it's the lossless raw codex format at a stable path -- the
# codex analogue of claude's projects/<session>.jsonl (and richer than the lossy
# common transcript we deliberately do NOT read). Kept as a local constant rather
# than importing the plugin, mirroring claude_session_parser's reimplement-don't-import
# stance toward mngr_claude's converter.
_RAW_TRANSCRIPT_RELATIVE = Path("logs") / "codex_transcript" / "events.jsonl"


class CodexSessionWatcher:
    """Watches a codex agent's raw rollout file and emits parsed UI events."""

    def __init__(
        self,
        agent_id: str,
        agent_state_dir: Path,
        on_events: Callable[[str, list[dict[str, Any]]], None],
    ) -> None:
        self._agent_id = agent_id
        self._transcript_path = agent_state_dir / _RAW_TRANSCRIPT_RELATIVE
        self._on_events = on_events

        # Guards the in-memory transcript mirror and the tail cursor. Held across
        # the (cheap, incremental) file read + adapt, but never across the
        # ``on_events`` fan-out callback -- the same discipline AgentSessionWatcher
        # follows.
        self._lock = threading.Lock()
        # Adapted UI events, in append (chronological) order.
        self._events: list[dict[str, Any]] = []
        # event_id -> index into _events, for O(1) offset lookup + dedup.
        self._event_index: dict[str, int] = {}
        # Bytes of the transcript file already consumed.
        self._byte_offset = 0
        # A trailing partial line (no newline yet) carried to the next read.
        self._partial = ""
        # Stable physical line counter -> event-id synthesis (each rollout line is at
        # most one UI event). Persisted across incremental reads; reset on truncation
        # with the rest of the cursor state.
        self._line_index = 0
        # call_id -> tool_name, so a function_call_output can recover its tool name
        # from the earlier function_call. Cross-line, persisted like the cursor.
        self._tool_name_by_call_id: dict[str, str] = {}

        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        # Watchdog observer on the transcript dir, so an append wakes the loop
        # immediately instead of waiting out the poll interval. Started lazily once
        # the dir exists (see _maybe_start_observer).
        self._observer: Observer | None = None

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
        watch_dir = self._transcript_path.parent
        if not watch_dir.is_dir():
            return
        try:
            observer = Observer()
            observer.schedule(WakeOnChangeHandler(self._wake_event), str(watch_dir), recursive=False)
            observer.start()
            self._observer = observer
        except OSError as e:
            logger.debug("codex watcher: failed to start watchdog on {}: {}", watch_dir, e)

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

    def _consume_new_lines(self) -> list[dict[str, Any]]:
        """Read bytes appended since the last cursor, adapt complete lines, return new events."""
        path = self._transcript_path
        try:
            size = path.stat().st_size
        except OSError:
            # File not created yet (agent hasn't produced a transcript) -- normal.
            return []

        new_events: list[dict[str, Any]] = []
        with self._lock:
            # Truncation / rotation: the file shrank, so our cursor is stale. Reset
            # and re-read from the start. The converter only ever appends, so this
            # is defensive, not expected.
            if size < self._byte_offset:
                self._events.clear()
                self._event_index.clear()
                self._byte_offset = 0
                self._partial = ""
                self._line_index = 0
                self._tool_name_by_call_id.clear()
            if size == self._byte_offset and not self._partial:
                return []

            try:
                with path.open("rb") as f:
                    f.seek(self._byte_offset)
                    raw = f.read()
            except OSError:
                logger.debug("codex watcher: failed to read {}", path)
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
                event = self._adapt_line(stripped, idx)
                if event is None:
                    continue
                event_id = event["event_id"]
                if event_id in self._event_index:
                    continue
                self._event_index[event_id] = len(self._events)
                self._events.append(event)
                new_events.append(event)

        return new_events

    def _adapt_line(self, line: str, line_index: int) -> dict[str, Any] | None:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("codex watcher: skipping malformed rollout line")
            return None
        if not isinstance(record, dict):
            return None
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
