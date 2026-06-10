"""Watch raw Claude session JSONL files for new events.

Uses watchdog for low-latency filesystem change detection with mtime-based
polling as a safety net fallback, following the pattern from watcher_common.py
in mngr_recursive.

Parsed events are held in a per-file append-only cache (``SessionFileState``).
Both the background poll loop and the synchronous ``get_all_events`` HTTP path
bring the cache up to date through the shared ``_ensure_cache_current`` helper,
which reads only the bytes appended since the last poll. This keeps the
transcript loader cheap for arbitrarily long conversations: a file is fully
parsed once, then only its growing tail is parsed on subsequent reads.

Because both paths share the cache, the poll loop must not infer "new events to
broadcast" from what its own parse produced -- a concurrent HTTP read may have
parsed the tail first. Instead each ``SessionFileState`` tracks an
``emitted_count``, and the poll loop emits every cached event past that marker,
guaranteeing each event reaches connected SSE clients exactly once.

On top of the cache the watcher links each parent Agent tool_call to the
subagent session it spawned, so the frontend can render a rich subagent card.
Linkage comes from two sources (see ``_enrich_subagent_metadata``): the
subagent's ``<id>.meta.json`` ``toolUseId`` (written at spawn time, so running
subagents link immediately) and the parent's tool_result ``subagent_id``
(authoritative but only available once the subagent finishes). Because a
subagent's linkage can land after its parent Agent tool_call was already
broadcast, unlinked parent events are cached and re-broadcast once linkage
appears (see ``_cache_unlinked_agent_parents`` /
``_rebroadcast_relinked_parents``), letting the frontend upgrade a plain
tool-call block into the rich card without a page refresh.

All access to the shared session collections, per-file caches, and subagent
linkage maps is guarded by ``_lock`` because the watcher thread and FastAPI
handler threads touch them concurrently. File I/O and parsing run while the
lock is held, but the ``on_events`` callback (which fans out to SSE queues) is
always invoked outside the lock to avoid serializing fan-out and to avoid
re-entrancy / deadlock.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any
from typing import Callable

from loguru import logger as _loguru_logger
from watchdog.observers import Observer

from imbue.system_interface.session_parser import parse_session_lines
from imbue.system_interface.watcher_common import POLL_INTERVAL_SECONDS
from imbue.system_interface.watcher_common import WakeOnChangeHandler

logger = _loguru_logger

_BRIEF_WAIT_SECONDS = 0.5


def _is_complete_json_object(fragment: bytes) -> bool:
    """Return whether ``fragment`` parses as a complete JSON value on its own.

    Used to decide whether a trailing line that lacks a newline terminator is a
    finished record written without a trailing ``\\n`` (parses -> complete) or an
    in-progress write that should be retained for the next read (does not parse).
    """
    stripped = fragment.strip()
    if not stripped:
        return False
    try:
        json.loads(stripped)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    return True


def _split_at_last_complete_line(data: bytes) -> tuple[bytes, bytes]:
    """Split raw appended bytes into ``(complete_lines, trailing_fragment)``.

    Only complete lines are safe to parse and consume: advancing the byte offset
    past an incomplete trailing line would lose that record permanently once it
    is finished, and decoding a boundary that splits a multi-byte UTF-8 sequence
    would corrupt it. The trailing fragment is returned so the caller can leave
    it unconsumed and re-read it on the next poll.

    A trailing fragment with no newline is treated as complete (folded into the
    first return value) only when it parses as JSON on its own -- a final record
    written without a trailing newline. Otherwise it is retained.
    """
    if data.endswith(b"\n"):
        return data, b""
    newline_index = data.rfind(b"\n")
    fragment = data if newline_index == -1 else data[newline_index + 1 :]
    if _is_complete_json_object(fragment):
        return data, b""
    if newline_index == -1:
        return b"", data
    return data[: newline_index + 1], fragment


class SessionFileState:
    """Tracks reading and parsed-events cache state for a single session JSONL file.

    ``byte_offset_consumed`` is the number of bytes through the last complete
    line that has been parsed into ``events`` (an append-only cache of all
    parsed events for the file). ``last_mtime`` lets the poller short-circuit
    when neither size nor mtime changed.

    ``emitted_count`` is the number of leading ``events`` already handed to the
    ``on_events`` SSE fan-out. It is tracked separately from parsing so the poll
    loop emits every not-yet-emitted event even when a concurrent HTTP
    ``get_all_events`` was the thread that actually parsed the new tail.
    """

    def __init__(self, session_id: str, file_path: Path) -> None:
        self.session_id = session_id
        self.file_path = file_path
        self.byte_offset_consumed: int = 0
        self.last_mtime: float = 0.0
        self.events: list[dict[str, Any]] = []
        self.emitted_count: int = 0


class AgentSessionWatcher:
    """Watches all session files for a single mngr agent and emits parsed events."""

    def __init__(
        self,
        agent_id: str,
        agent_state_dir: Path,
        claude_config_dir: Path,
        on_events: Callable[[str, list[dict[str, Any]]], None],
    ) -> None:
        self._agent_id = agent_id
        self._agent_state_dir = agent_state_dir
        self._claude_config_dir = claude_config_dir
        self._on_events = on_events

        # Guards every shared collection below, plus every SessionFileState.
        # Held across file I/O and parsing (cheap, incremental, per-agent) but
        # never across the on_events fan-out callback.
        self._lock = threading.Lock()
        self._session_states: dict[str, SessionFileState] = {}
        self._known_session_ids: list[str] = []
        self._main_session_ids: list[str] = []
        self._tool_name_by_call_id: dict[str, str] = {}
        self._existing_event_ids: set[str] = set()
        self._subagent_metadata: dict[str, dict[str, str]] = {}  # sub_id -> {agent_type, description}
        # sub_ids whose meta.json we've already determined is permanently malformed.
        # Used to log the warning once per file instead of once per poll cycle.
        self._subagent_meta_read_failed: set[str] = set()
        # sub_id -> the parent Agent tool_use id, read from the subagent's `<id>.meta.json`
        # `toolUseId` field. This is the direct, spawn-time link between a parent Agent
        # tool_call and its subagent -- written before any tool_result lands -- so running
        # subagents get the rich card. (Claude Code does NOT put a usable parent pointer in
        # the subagent jsonl's first line: `parentUuid` is null and `sourceToolAssistantUUID`
        # is absent, so the meta.json is the only pre-completion source.)
        self._subagent_tool_use_id: dict[str, str] = {}
        # tool_call_id -> subagent_id, accumulated from parent tool_results as they stream in.
        # Persistent (like _subagent_tool_use_id) so a parent assistant message broadcast in an
        # earlier poll cycle can be re-linked once its subagent's tool_result lands in a later
        # cycle, rather than only on a full re-parse (page refresh). This is the fallback that
        # links sessions recorded on Claude Code versions whose meta.json omits toolUseId; it
        # resolves the click-through when the subagent finishes (the card itself renders from
        # the tool call's description/subagent_type as soon as the call appears).
        self._subagent_id_by_tool_call: dict[str, str] = {}
        # message_uuid -> assistant_message event that was streamed with at least one Agent
        # tool_call still missing its subagent_metadata. A subagent's jsonl (and thus its
        # parent linkage) can appear after the parent was already broadcast, so we keep the
        # event around to re-enrich and re-broadcast it once linkage lands (see
        # _rebroadcast_relinked_parents). Fully-linked parents are never cached.
        self._unlinked_agent_parent_events: dict[str, dict[str, Any]] = {}

        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._observer: Any = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start watching session files in a background thread."""
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"watcher-{self._agent_id}")
        self._thread.start()

    def stop(self) -> None:
        """Stop watching."""
        self._stop_event.set()
        self._wake_event.set()
        if self._observer is not None:
            self._observer.stop()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def get_all_events(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """Read session files and return parsed events.

        Args:
            session_id: If provided, only return events from this session.
                If None, return events from main sessions only (not subagents).
        """
        self._discover_sessions()

        with self._lock:
            states = list(self._session_states.values())
            main_session_ids = set(self._main_session_ids)

        selected_states: list[SessionFileState] = []
        for state in states:
            if not state.file_path.exists():
                continue
            if session_id is not None and state.session_id != session_id:
                continue
            if session_id is None and state.session_id not in main_session_ids:
                continue
            selected_states.append(state)

        all_events: list[dict[str, Any]] = []
        for state in selected_states:
            self._ensure_cache_current(state)
            with self._lock:
                all_events.extend(state.events)

        all_events.sort(key=lambda e: e.get("timestamp", ""))
        self._enrich_subagent_metadata(all_events)
        return all_events

    def get_backfill_events(
        self, before_event_id: str, limit: int = 50, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Get events before a given event_id for backfill pagination."""
        all_events = self.get_all_events(session_id=session_id)

        target_idx = -1
        for i, event in enumerate(all_events):
            if event["event_id"] == before_event_id:
                target_idx = i
                break

        if target_idx <= 0:
            return []

        start_idx = max(0, target_idx - limit)
        return all_events[start_idx:target_idx]

    def get_subagent_metadata(self, subagent_session_id: str) -> dict[str, str] | None:
        """Get metadata for a subagent by its session ID."""
        self._discover_sessions()
        with self._lock:
            return self._subagent_metadata.get(subagent_session_id)

    def is_main_session_event(self, event: dict[str, Any]) -> bool:
        """True if an event belongs to a main session rather than a subagent session.

        Events with no ``session_id`` (e.g. plugin-injected application events) are
        treated as main so they keep reaching the main stream. Subagent-session events
        are delivered only through the per-subagent stream, so the main stream must
        drop them -- otherwise a running subagent's own prompt, tool calls, and
        assistant messages would render inline in the parent thread.
        """
        session_id = event.get("session_id")
        if session_id is None:
            return True
        with self._lock:
            return session_id in self._main_session_ids

    def _ensure_cache_current(self, state: SessionFileState, mark_all_emitted: bool = False) -> None:
        """Bring ``state``'s cache up to the file's current contents under the lock.

        Appends any newly parsed events to ``state.events`` (the full accumulated
        transcript). Emission to SSE clients is decoupled from parsing: callers
        that need to broadcast deltas drive that off ``state.emitted_count`` so a
        concurrent HTTP read parsing the tail does not rob the poll loop of the
        events to emit.

        When ``mark_all_emitted`` is set (the priming path), the whole current
        backlog is marked as already emitted *in the same lock hold* that filled
        the cache. Splitting the fill and the mark across two lock acquisitions
        would let a concurrent ``get_all_events`` append events in between that
        then get marked emitted and never reach SSE clients.
        """
        with self._lock:
            try:
                stat = state.file_path.stat()
            except OSError as e:
                logger.debug("Failed to stat session file {}: {}", state.file_path, e)
                return

            current_size = stat.st_size
            current_mtime = stat.st_mtime

            # Truncation / rotation: the file shrank below what we have already
            # consumed, so our offset is stale. Reset and re-read from the start.
            # Discard this file's event IDs from the agent-wide dedup set first;
            # otherwise the re-read would be deduplicated against the stale
            # pre-truncation IDs and silently drop every record whose ID recurs
            # (the typical atomic save-rewrite case), leaving the cache empty.
            if current_size < state.byte_offset_consumed:
                for event in state.events:
                    self._existing_event_ids.discard(event["event_id"])
                state.byte_offset_consumed = 0
                state.events = []
                # The re-read content must be re-emitted to live SSE clients, so
                # the emission marker resets alongside the cache.
                state.emitted_count = 0

            if current_size == state.byte_offset_consumed and current_mtime == state.last_mtime:
                if mark_all_emitted:
                    state.emitted_count = len(state.events)
                return

            try:
                with open(state.file_path, "rb") as f:
                    f.seek(state.byte_offset_consumed)
                    new_data = f.read()
            except OSError as e:
                logger.debug("Failed to read session file {}: {}", state.file_path, e)
                return

            complete, _fragment = _split_at_last_complete_line(new_data)
            if not complete:
                # Only a partial trailing line so far; leave the offset where it
                # is and re-read on the next poll once the writer flushes.
                return

            try:
                decoded = complete.decode("utf-8")
            except UnicodeDecodeError as e:
                logger.warning("UTF-8 decode error in session file {}: {}", state.file_path, e)
                return

            new_events = parse_session_lines(
                decoded.splitlines(),
                existing_event_ids=self._existing_event_ids,
                tool_name_by_call_id=self._tool_name_by_call_id,
                session_id=state.session_id,
            )
            state.byte_offset_consumed += len(complete)
            state.last_mtime = current_mtime
            state.events.extend(new_events)
            if mark_all_emitted:
                state.emitted_count = len(state.events)

    def _enrich_subagent_metadata(self, events: list[dict[str, Any]]) -> None:
        """Enrich Agent tool_use events with subagent metadata.

        Builds a tool_call_id -> subagent_id map from two sources, highest
        precedence first (the second only fills tool calls the first left
        unresolved):

        1. ``toolUseId`` from each subagent's `<id>.meta.json`, which names the
           parent Agent tool_use directly. Written at spawn time, so it links
           running subagents immediately. Emitted by the pinned Claude Code
           version (see CLAUDE_CODE_VERSION in the Dockerfile).

        2. ``subagent_id`` from the parent's tool_result (structured
           `toolUseResult.agentId` or the legacy `agentId:` trailer), accumulated
           persistently across poll cycles. Authoritative but only available once
           the subagent finishes; the fallback for sessions recorded on older
           Claude Code versions whose meta.json predates `toolUseId`, or whose
           subagent files are no longer on disk.

        Accumulating tool_result linkage mutates shared maps, and the metadata
        lookups read shared maps, so the whole body runs under ``_lock``. The
        ``events`` list and its dicts are not handed to ``on_events`` from here,
        so holding the lock across the in-memory enrichment cannot deadlock on
        the fan-out.
        """
        with self._lock:
            subagent_by_tool_call: dict[str, str] = {}

            # 1. Disk-based linkage: each subagent meta.json's toolUseId names its parent
            #    Agent tool_use directly. sub_id is the jsonl stem ("agent-<id>").
            for sub_id, tool_use_id in self._subagent_tool_use_id.items():
                subagent_by_tool_call[tool_use_id] = sub_id

            # 2. Accumulate tool_result-based linkage from this batch into the persistent map,
            #    then resolve against the full accumulated map (not just this batch's events),
            #    so the rebroadcast pass links a cached parent against a tool_result that
            #    arrived in a different poll cycle.
            for event in events:
                if event.get("type") != "tool_result":
                    continue
                if "subagent_id" not in event:
                    continue
                self._subagent_id_by_tool_call.setdefault(event["tool_call_id"], event["subagent_id"])
            for tool_call_id, subagent_id in self._subagent_id_by_tool_call.items():
                subagent_by_tool_call.setdefault(tool_call_id, subagent_id)

            # Enrich assistant messages that have Agent tool calls.
            for event in events:
                if event.get("type") != "assistant_message":
                    continue
                tool_calls = event.get("tool_calls", [])
                for tc in tool_calls:
                    if tc.get("tool_name") != "Agent":
                        continue
                    sub_id = subagent_by_tool_call.get(tc["tool_call_id"])
                    if not sub_id:
                        continue
                    # The agentId in tool results is bare (e.g. "af25b729465418580")
                    # but session files are named "agent-af25b729465418580.jsonl",
                    # so metadata is keyed by "agent-<id>". Try both forms.
                    metadata = self._subagent_metadata.get(sub_id) or self._subagent_metadata.get(f"agent-{sub_id}")
                    if metadata:
                        tc["subagent_metadata"] = metadata

    def _run(self) -> None:
        """Main watcher loop."""
        self._discover_sessions()
        self._setup_watchers()
        self._prime_caches()

        while not self._stop_event.is_set():
            self._wake_event.wait(timeout=POLL_INTERVAL_SECONDS)
            self._wake_event.clear()

            if self._stop_event.is_set():
                break

            # Order matters: discover refreshes the meta.json/toolUseId caches, poll
            # reads new events (accumulating tool_result linkage and caching new unlinked
            # parents), and rebroadcast runs last so it re-links cached parents against the
            # caches as they stand after BOTH -- letting a tool_result that lands this cycle
            # upgrade an older parent's card in the same cycle.
            self._discover_sessions()
            self._poll_for_changes()
            self._rebroadcast_relinked_parents()

        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5.0)

    def _prime_caches(self) -> None:
        """Parse the existing backlog into each cache without emitting it.

        The initial transcript is delivered to clients via the REST
        ``get_all_events`` path, so the watcher must not also broadcast the
        backlog through ``on_events`` (that would flood the bounded SSE queues
        for long histories). Priming fills ``cache.events`` and advances the
        byte offset to EOF, then marks the whole backlog as already emitted so
        the poll loop afterwards emits only events appended after start.
        """
        with self._lock:
            states = list(self._session_states.values())
        for state in states:
            if state.file_path.exists():
                # Fill the cache and mark the whole backlog emitted atomically so
                # a concurrent get_all_events cannot slip in events that then get
                # marked emitted and never reach SSE clients.
                self._ensure_cache_current(state, mark_all_emitted=True)

    def _discover_sessions(self) -> None:
        """Read claude_session_id_history to find all session IDs."""
        history_file = self._agent_state_dir / "claude_session_id_history"
        if not history_file.exists():
            return

        try:
            lines = history_file.read_text().splitlines()
        except OSError as e:
            logger.debug("Failed to read session history file {}: {}", history_file, e)
            return

        for line in lines:
            parts = line.strip().split()
            if not parts:
                continue
            session_id = parts[0]
            with self._lock:
                already_known = session_id in self._session_states
            if already_known:
                continue

            # Try to find the session file
            file_path = self._find_session_file(session_id)
            if file_path is None:
                # Brief wait then try again
                time.sleep(_BRIEF_WAIT_SECONDS)
                file_path = self._find_session_file(session_id)
                if file_path is None:
                    logger.debug("Session file not found for {}, will retry on next cycle", session_id)
                    continue

            with self._lock:
                if session_id in self._session_states:
                    continue
                self._session_states[session_id] = SessionFileState(session_id, file_path)
                self._known_session_ids.append(session_id)
                self._main_session_ids.append(session_id)

            # Set up watchdog for the new file
            if self._observer is not None:
                parent_dir = str(file_path.parent)
                try:
                    self._observer.schedule(WakeOnChangeHandler(self._wake_event), parent_dir, recursive=False)
                except OSError as e:
                    logger.debug("Failed to schedule watchdog for {}: {}", parent_dir, e)

        # Discover subagent sessions for ALL known sessions (not just newly discovered ones),
        # since subagent files may appear after the parent session is first discovered.
        with self._lock:
            states = list(self._session_states.values())
        for state in states:
            self._discover_subagent_sessions(state.session_id, state.file_path)

    def _discover_subagent_sessions(self, parent_session_id: str, parent_file_path: Path) -> None:
        """Discover subagent session files under <session_id>/subagents/.

        Registers each subagent's jsonl into the cache-model ``_session_states`` and
        reads its ``<id>.meta.json`` for display metadata plus the ``toolUseId`` that
        links it to its parent Agent tool_call.
        """
        subagents_dir = parent_file_path.parent / parent_session_id / "subagents"
        if not subagents_dir.exists():
            return

        for jsonl_file in subagents_dir.glob("*.jsonl"):
            sub_id = jsonl_file.stem

            with self._lock:
                is_new_session = sub_id not in self._session_states
                if is_new_session:
                    self._session_states[sub_id] = SessionFileState(sub_id, jsonl_file)
                    self._known_session_ids.append(sub_id)
                meta_already_resolved = sub_id in self._subagent_metadata or sub_id in self._subagent_meta_read_failed

            if is_new_session and self._observer is not None:
                try:
                    self._observer.schedule(WakeOnChangeHandler(self._wake_event), str(subagents_dir), recursive=False)
                except OSError as e:
                    logger.debug("Failed to schedule watchdog for {}: {}", subagents_dir, e)

            # Cache .meta.json. Retry on each pass while the read fails with OSError
            # (transient: mid-write, momentary permission glitch). Give up after a
            # JSONDecodeError (truly malformed -- won't self-heal) so we don't spam
            # the log on every poll cycle.
            if meta_already_resolved:
                continue
            meta_file = jsonl_file.with_suffix(".meta.json")
            if not meta_file.exists():
                continue
            try:
                meta = json.loads(meta_file.read_text())
            except json.JSONDecodeError as exc:
                logger.warning("Subagent meta.json is not valid JSON, giving up: {}: {}", meta_file, exc)
                with self._lock:
                    self._subagent_meta_read_failed.add(sub_id)
                continue
            except OSError as exc:
                logger.debug("Failed to read subagent meta.json {}: {}", meta_file, exc)
                continue

            # toolUseId points directly at the parent Agent tool_use, giving the
            # running subagent its rich card before any tool_result lands. Absent
            # on older Claude Code versions, which fall back to tool_result linkage.
            tool_use_id = meta.get("toolUseId")
            with self._lock:
                if sub_id not in self._subagent_metadata:
                    self._subagent_metadata[sub_id] = {
                        "agent_type": meta.get("agentType", ""),
                        "description": meta.get("description", ""),
                        "session_id": sub_id,
                    }
                if isinstance(tool_use_id, str) and tool_use_id:
                    self._subagent_tool_use_id[sub_id] = tool_use_id

    def _find_session_file(self, session_id: str) -> Path | None:
        """Search for a session JSONL file under the Claude projects directory."""
        projects_dir = self._claude_config_dir / "projects"
        if not projects_dir.exists():
            return None

        # Walk the projects directory looking for the session file
        target_name = f"{session_id}.jsonl"
        for root, _dirs, files in os.walk(str(projects_dir)):
            if target_name in files:
                return Path(root) / target_name
        return None

    def _setup_watchers(self) -> None:
        """Set up watchdog observers for known session file directories."""
        watched_dirs: set[str] = set()
        with self._lock:
            states = list(self._session_states.values())
        for state in states:
            if state.file_path.exists():
                watched_dirs.add(str(state.file_path.parent))

        # Also watch the history file's directory
        history_file = self._agent_state_dir / "claude_session_id_history"
        if history_file.parent.exists():
            watched_dirs.add(str(history_file.parent))

        if not watched_dirs:
            return

        try:
            observer = Observer()
            handler = WakeOnChangeHandler(self._wake_event)
            for dir_path in watched_dirs:
                observer.schedule(handler, dir_path, recursive=False)
            observer.start()
            self._observer = observer
        except OSError as e:
            logger.debug("Failed to start watchdog observer, falling back to polling only: {}", e)

    def _poll_for_changes(self) -> None:
        """Check all session files for new content and emit any not-yet-emitted events.

        Emission is driven by ``emitted_count`` rather than by what this call
        parsed, so events that a concurrent HTTP ``get_all_events`` parsed into
        the cache are still delivered to connected SSE clients exactly once.

        Each batch is enriched with subagent metadata before broadcast, and any
        Agent-parent events still missing linkage are cached so a later cycle can
        re-broadcast them once linkage lands (see ``_rebroadcast_relinked_parents``).
        """
        with self._lock:
            states = list(self._session_states.values())

        for state in states:
            if not state.file_path.exists():
                continue

            self._ensure_cache_current(state)
            with self._lock:
                pending_events = state.events[state.emitted_count :]
                state.emitted_count = len(state.events)
            if pending_events:
                self._enrich_subagent_metadata(pending_events)
                self._cache_unlinked_agent_parents(pending_events)
                self._on_events(self._agent_id, pending_events)

    def _cache_unlinked_agent_parents(self, events: list[dict[str, Any]]) -> None:
        """Remember assistant messages whose Agent tool_calls aren't linked yet.

        When an Agent tool_call is broadcast before its subagent's linkage exists, it goes
        out without subagent_metadata. We keep the event so a later cycle can re-enrich and
        re-broadcast it once the linkage lands (see _rebroadcast_relinked_parents). Fully
        linked parents are skipped -- there is nothing left to resolve.
        """
        for event in events:
            if event.get("type") != "assistant_message":
                continue
            agent_tool_calls = [tc for tc in event.get("tool_calls", []) if tc.get("tool_name") == "Agent"]
            if not agent_tool_calls:
                continue
            if self._is_fully_linked(event):
                continue
            message_uuid = event.get("message_uuid", "")
            if message_uuid:
                with self._lock:
                    self._unlinked_agent_parent_events[message_uuid] = event

    def _rebroadcast_relinked_parents(self) -> None:
        """Re-emit cached parent events that gained subagent links since broadcast.

        A subagent's linkage can appear after the parent Agent tool_call was already
        streamed: the subagent's meta.json (with toolUseId) shows up a cycle later, or its
        tool_result lands later still. Re-enriching the cached parent and re-broadcasting it
        once it links lets the frontend upgrade the plain tool-call block into the rich card
        without a page refresh. Parents whose Agent tool_calls are all linked are dropped
        from the cache.

        The cache snapshot and removals run under ``_lock`` (they mutate shared state), but
        ``_enrich_subagent_metadata`` / ``_is_fully_linked`` take the lock themselves and the
        ``on_events`` fan-out runs unlocked, so the lock is never held across either.
        """
        with self._lock:
            cached = list(self._unlinked_agent_parent_events.items())

        relinked: list[dict[str, Any]] = []
        fully_linked_uuids: list[str] = []
        for message_uuid, event in cached:
            before = self._linked_agent_tool_call_ids(event)
            self._enrich_subagent_metadata([event])
            if self._linked_agent_tool_call_ids(event) != before:
                relinked.append(event)
            if self._is_fully_linked(event):
                fully_linked_uuids.append(message_uuid)

        if fully_linked_uuids:
            with self._lock:
                for message_uuid in fully_linked_uuids:
                    self._unlinked_agent_parent_events.pop(message_uuid, None)

        if relinked:
            self._on_events(self._agent_id, relinked)

    @staticmethod
    def _linked_agent_tool_call_ids(event: dict[str, Any]) -> frozenset[str]:
        """tool_call_ids of Agent tool_calls in ``event`` that already carry metadata."""
        return frozenset(
            tc.get("tool_call_id", "")
            for tc in event.get("tool_calls", [])
            if tc.get("tool_name") == "Agent" and "subagent_metadata" in tc
        )

    def _is_fully_linked(self, event: dict[str, Any]) -> bool:
        """True if every Agent tool_call in ``event`` is linked to a subagent.

        Linkage is resolved by toolUseId (meta.json) or tool_result agentId; both are read
        from disk. A tool_call counts as linked once it appears in either source's map, even
        if no metadata could be attached (e.g. the subagent's files were cleaned up), so such
        a parent is not retried forever.
        """
        with self._lock:
            linked = set(self._subagent_tool_use_id.values()) | set(self._subagent_id_by_tool_call.keys())
        agent_tool_calls = [tc for tc in event.get("tool_calls", []) if tc.get("tool_name") == "Agent"]
        return bool(agent_tool_calls) and all(tc.get("tool_call_id") in linked for tc in agent_tool_calls)
