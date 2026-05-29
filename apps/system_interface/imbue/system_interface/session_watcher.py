"""Watch raw Claude session JSONL files for new events.

Uses watchdog for low-latency filesystem change detection with mtime-based
polling as a safety net fallback, following the pattern from watcher_common.py
in mngr_recursive.

Scaling model (two-tier cache)
------------------------------
A conversation transcript is conceptually unbounded, so the watcher never holds
every parsed event body in memory. Each session file keeps two tiers:

* **Locator tier** (``SessionFileState.locators``): one small ``EventLocator``
  per event -- ``event_id``, ``timestamp`` and the byte range of the source
  JSONL line. Built incrementally as the file is tailed and never evicted. It
  is O(total events) in *count* but free of message/tool bodies (tens of bytes
  per event), and lives only in memory -- there is no on-disk index sidecar.

* **Body tier** (``_body_cache``): a bounded LRU of fully parsed event dicts
  (the heavy ``text`` / ``output`` / ``tool_calls`` payloads). When a requested
  event is not resident, its source line is re-read from disk via the locator's
  byte range and re-parsed. Backend memory is therefore bounded by the cache
  capacity plus the compact locator index, regardless of transcript length.

The bounded read paths (:meth:`get_tail_events`, :meth:`get_backfill_events`)
locate events through the locator index and resolve at most ``limit`` bodies, so
a backfill page costs O(limit) disk work rather than re-reading the whole file.
:meth:`get_all_events` remains for the bounded subagent transcripts and as a
compatibility/oracle path; it resolves every body and is not on the main-view
hot path.

All access to the shared session collections, per-file locator lists and the
body cache is guarded by ``_lock`` because the watcher thread and FastAPI
handler threads touch them concurrently. File I/O and parsing run while the lock
is held, but the ``on_events`` callback (which fans out to SSE queues) is always
invoked outside the lock to avoid serializing fan-out and to avoid re-entrancy.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any
from typing import Callable

from loguru import logger as _loguru_logger
from watchdog.events import FileSystemEvent
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from imbue.system_interface.session_parser import parse_session_lines

logger = _loguru_logger

_NON_CHANGE_EVENT_TYPES = frozenset({"opened", "closed", "closed_no_write"})

_POLL_INTERVAL_SECONDS = 1.0
_BRIEF_WAIT_SECONDS = 0.5

# Maximum number of parsed event bodies held resident across all of an agent's
# session files. Far larger than any single tail/backfill page (default 50), so
# normal scrollback stays in-cache, while still bounding memory for an
# arbitrarily long transcript. Bodies evicted past this are re-parsed from disk
# on demand via the locator byte ranges.
_DEFAULT_BODY_CACHE_CAPACITY = 2000


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


def _iter_line_spans(data: bytes, base_offset: int) -> list[tuple[int, int, bytes]]:
    """Split ``data`` into ``(byte_offset, byte_len, line_bytes)`` per line.

    ``byte_offset`` is the absolute file offset of the line (``base_offset`` plus
    the line's position within ``data``); ``byte_len`` is the line length in
    bytes including its trailing newline (the final line may lack one). The line
    bytes include the newline so re-reading exactly ``byte_len`` bytes at
    ``byte_offset`` reproduces the line.
    """
    spans: list[tuple[int, int, bytes]] = []
    pos = 0
    length = len(data)
    while pos < length:
        newline_index = data.find(b"\n", pos)
        if newline_index == -1:
            line = data[pos:]
            next_pos = length
        else:
            line = data[pos : newline_index + 1]
            next_pos = newline_index + 1
        spans.append((base_offset + pos, len(line), line))
        pos = next_pos
    return spans


class EventLocator(tuple[str, str, int, int]):
    """A bodyless pointer to a single parsed event within a session file.

    ``byte_offset`` / ``byte_len`` address the source JSONL *line* (a line may
    yield more than one event, so several locators can share a byte range).
    Re-reading those bytes and re-parsing reconstructs the event body.

    A ``tuple`` subclass with ``__slots__ = ()`` so each instance is as small as
    a plain 4-tuple (no ``__dict__``): there is one locator per event for the
    whole unbounded transcript, so keeping the per-event footprint minimal is
    the point of the locator tier. The property accessors give named, readable
    access without the per-instance overhead of a model.
    """

    __slots__ = ()

    def __new__(cls, event_id: str, timestamp: str, byte_offset: int, byte_len: int) -> EventLocator:
        return super().__new__(cls, (event_id, timestamp, byte_offset, byte_len))

    @property
    def event_id(self) -> str:
        return self[0]

    @property
    def timestamp(self) -> str:
        return self[1]

    @property
    def byte_offset(self) -> int:
        return self[2]

    @property
    def byte_len(self) -> int:
        return self[3]


class _ChangeHandler(FileSystemEventHandler):
    """Watchdog handler that wakes the watcher on actual file changes."""

    def __init__(self, wake_event: threading.Event) -> None:
        self._wake_event = wake_event

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type in _NON_CHANGE_EVENT_TYPES:
            return
        self._wake_event.set()


class SessionFileState:
    """Tracks reading and locator state for a single session JSONL file.

    ``byte_offset_consumed`` is the number of bytes through the last complete
    line that has been parsed into ``locators`` (the append-only locator index
    for the file). ``last_mtime`` lets the poller short-circuit when neither size
    nor mtime changed. Parsed event *bodies* are not stored here -- they live in
    the watcher's bounded body cache.
    """

    def __init__(self, session_id: str, file_path: Path) -> None:
        self.session_id = session_id
        self.file_path = file_path
        self.byte_offset_consumed: int = 0
        self.last_mtime: float = 0.0
        self.locators: list[EventLocator] = []


class AgentSessionWatcher:
    """Watches all session files for a single mngr agent and emits parsed events."""

    def __init__(
        self,
        agent_id: str,
        agent_state_dir: Path,
        claude_config_dir: Path,
        on_events: Callable[[str, list[dict[str, Any]]], None],
        body_cache_capacity: int = _DEFAULT_BODY_CACHE_CAPACITY,
    ) -> None:
        self._agent_id = agent_id
        self._agent_state_dir = agent_state_dir
        self._claude_config_dir = claude_config_dir
        self._on_events = on_events
        self._body_cache_capacity = body_cache_capacity

        # Guards _session_states, _main_session_ids, _tool_name_by_call_id,
        # _existing_event_ids, _subagent_metadata, _subagent_id_by_tool_call,
        # _body_cache, _locator_ref_by_id, and every SessionFileState. Held
        # across file I/O and parsing (cheap, incremental, per-agent) but never
        # across the on_events fan-out callback.
        self._lock = threading.Lock()
        self._session_states: dict[str, SessionFileState] = {}
        self._main_session_ids: list[str] = []
        self._tool_name_by_call_id: dict[str, str] = {}
        self._existing_event_ids: set[str] = set()
        self._subagent_metadata: dict[str, dict[str, str]] = {}  # sub_id -> {agent_type, description}
        # Maps tool_call_id -> subagent session id, populated as tool_result
        # events are parsed, so metadata enrichment works across windows.
        self._subagent_id_by_tool_call: dict[str, str] = {}

        # Bounded LRU of parsed event bodies, keyed by event_id, plus a bodyless
        # locator -> position index so any event can be located and re-resolved.
        self._body_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._locator_ref_by_id: dict[str, tuple[SessionFileState, int]] = {}

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
        """Read session files and return every parsed event, sorted by timestamp.

        Resolves all event bodies (re-parsing any evicted from the body cache),
        so this is O(total events) and is intended for the bounded subagent
        transcripts and as a compatibility/oracle path -- the main-view hot path
        uses :meth:`get_tail_events` / :meth:`get_backfill_events`.

        Args:
            session_id: If provided, only return events from this session.
                If None, return events from main sessions only (not subagents).
        """
        states = self._selected_states_current(session_id)

        with self._lock:
            pairs: list[tuple[SessionFileState, EventLocator]] = []
            for state in states:
                for locator in state.locators:
                    pairs.append((state, locator))
            pairs.sort(key=lambda pair: pair[1].timestamp)
            events = self._resolve_bodies_locked(pairs)

        self._enrich_subagent_metadata(events)
        return events

    def get_tail_events(self, limit: int, session_id: str | None = None) -> list[dict[str, Any]]:
        """Return the most recent ``limit`` events (chronological order).

        Bounded: walks only the tail of the locator index and resolves at most
        ``limit`` event bodies, never reading the whole transcript.
        """
        if limit <= 0:
            return []
        states = self._selected_states_current(session_id)

        with self._lock:
            tail = self._collect_tail_locked(states, limit)
            events = self._resolve_bodies_locked(tail)

        self._enrich_subagent_metadata(events)
        return events

    def get_backfill_events(
        self, before_event_id: str, limit: int = 50, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Return up to ``limit`` events immediately before ``before_event_id``.

        Bounded: the target is located through the ``_locator_ref_by_id`` index
        (O(1)) and at most ``limit`` bodies are resolved (re-read from disk on a
        cache miss), so a page costs O(limit) regardless of how far back it
        reaches.
        """
        if limit <= 0:
            return []
        states = self._selected_states_current(session_id)

        with self._lock:
            page = self._collect_before_locked(states, before_event_id, limit)
            events = self._resolve_bodies_locked(page)

        self._enrich_subagent_metadata(events)
        return events

    def has_events_before(self, event_id: str, session_id: str | None = None) -> bool:
        """Whether any event precedes ``event_id`` in the selected timeline.

        Used to populate the ``has_more`` flag for tail/backfill responses
        without resolving any bodies.
        """
        states = self._selected_states_current(session_id)
        with self._lock:
            ref = self._locator_ref_by_id.get(event_id)
            if ref is None:
                return False
            ref_state, ref_idx = ref
            if ref_idx > 0:
                return True
            try:
                state_pos = states.index(ref_state)
            except ValueError:
                return False
            return any(states[pos].locators for pos in range(state_pos))

    def get_subagent_metadata(self, subagent_session_id: str) -> dict[str, str] | None:
        """Get metadata for a subagent by its session ID."""
        self._discover_sessions()
        with self._lock:
            return self._subagent_metadata.get(subagent_session_id)

    def _selected_states_current(self, session_id: str | None) -> list[SessionFileState]:
        """Discover sessions, bring the selected files' caches current, return them.

        The returned list is ordered chronologically by session (main sessions in
        history order; a single session when ``session_id`` is given). Cache
        refresh happens outside the returned-list construction so callers can take
        the lock once for the read phase.
        """
        self._discover_sessions()

        with self._lock:
            states = self._ordered_selected_states_locked(session_id)

        # _ensure_cache_current locks internally; call it outside the lock above.
        for state in states:
            if state.file_path.exists():
                self._ensure_cache_current(state)
        return states

    def _ordered_selected_states_locked(self, session_id: str | None) -> list[SessionFileState]:
        """Return the selected session states in chronological order (lock held)."""
        if session_id is not None:
            state = self._session_states.get(session_id)
            return [state] if state is not None else []
        # Main sessions in history (chronological) order. Resumed sessions do not
        # overlap in time, so this order matches the merged timestamp order.
        ordered: list[SessionFileState] = []
        for sid in self._main_session_ids:
            state = self._session_states.get(sid)
            if state is not None:
                ordered.append(state)
        return ordered

    def _collect_tail_locked(
        self, states: list[SessionFileState], limit: int
    ) -> list[tuple[SessionFileState, EventLocator]]:
        """Collect the last ``limit`` locators across ``states`` (chronological, lock held).

        Walks states from newest to oldest, taking only as many locators as
        needed from each file's tail -- O(limit + file count), never O(total events).
        """
        collected: list[tuple[SessionFileState, EventLocator]] = []
        needed = limit
        pos = len(states) - 1
        while needed > 0 and pos >= 0:
            locators = states[pos].locators
            start = max(0, len(locators) - needed)
            chunk = [(states[pos], locator) for locator in locators[start:]]
            collected = chunk + collected
            needed -= len(locators) - start
            pos -= 1
        return collected

    def _collect_before_locked(
        self, states: list[SessionFileState], before_event_id: str, limit: int
    ) -> list[tuple[SessionFileState, EventLocator]]:
        """Collect up to ``limit`` locators immediately before ``before_event_id`` (lock held).

        Locates the target via ``_locator_ref_by_id`` (O(1)) then walks backward
        across the selected files -- O(limit + file count). Returns [] if the target
        is unknown, is the very first event, or is not in the selected sessions.
        """
        ref = self._locator_ref_by_id.get(before_event_id)
        if ref is None:
            return []
        ref_state, ref_idx = ref
        try:
            state_pos = states.index(ref_state)
        except ValueError:
            return []

        collected: list[tuple[SessionFileState, EventLocator]] = []
        needed = limit
        pos = state_pos
        while needed > 0 and pos >= 0:
            state = states[pos]
            end = ref_idx if state is ref_state else len(state.locators)
            start = max(0, end - needed)
            chunk = [(state, locator) for locator in state.locators[start:end]]
            collected = chunk + collected
            needed -= end - start
            pos -= 1
        return collected

    def _cache_put_locked(self, event: dict[str, Any]) -> None:
        """Insert/refresh an event body in the LRU, evicting the oldest if over capacity."""
        event_id = event["event_id"]
        self._body_cache[event_id] = event
        self._body_cache.move_to_end(event_id)
        while len(self._body_cache) > self._body_cache_capacity:
            self._body_cache.popitem(last=False)

    def _resolve_bodies_locked(self, pairs: list[tuple[SessionFileState, EventLocator]]) -> list[dict[str, Any]]:
        """Resolve event bodies for ``pairs``, re-reading from disk on a miss (lock held)."""
        events: list[dict[str, Any]] = []
        for state, locator in pairs:
            body = self._body_cache.get(locator.event_id)
            if body is not None:
                self._body_cache.move_to_end(locator.event_id)
            else:
                for reparsed in self._reparse_line_locked(state, locator):
                    self._cache_put_locked(reparsed)
                body = self._body_cache.get(locator.event_id)
            if body is not None:
                events.append(body)
        return events

    def _reparse_line_locked(self, state: SessionFileState, locator: EventLocator) -> list[dict[str, Any]]:
        """Re-read and parse the single source line a locator points at (lock held).

        Parses with deduplication disabled so the body is reconstructed even
        though its ID is already in the agent-wide seen-set, and reuses the
        persistent tool-name map so re-parsed tool_result events keep their tool
        names. Returns every event the line yields (a line may produce several).
        """
        try:
            with open(state.file_path, "rb") as f:
                f.seek(locator.byte_offset)
                raw = f.read(locator.byte_len)
        except OSError as e:
            logger.debug("Failed to re-read session line {}@{}: {}", state.file_path, locator.byte_offset, e)
            return []
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            logger.warning("UTF-8 decode error re-reading {}@{}: {}", state.file_path, locator.byte_offset, e)
            return []
        return parse_session_lines(
            decoded.splitlines(),
            existing_event_ids=None,
            tool_name_by_call_id=self._tool_name_by_call_id,
            session_id=state.session_id,
        )

    def _ensure_cache_current(self, state: SessionFileState) -> list[dict[str, Any]]:
        """Bring ``state``'s locator index up to the file's current contents (lock held internally).

        Returns the events newly parsed by this call (empty if nothing changed),
        so a caller acting as the live source -- the poll loop -- can emit only
        the genuinely new events. New bodies are also written into the LRU cache.
        """
        with self._lock:
            try:
                stat = state.file_path.stat()
            except OSError as e:
                logger.debug("Failed to stat session file {}: {}", state.file_path, e)
                return []

            current_size = stat.st_size
            current_mtime = stat.st_mtime

            # Truncation / rotation: the file shrank below what we have already
            # consumed, so our offset is stale. Reset and re-read from the start.
            # Purge this file's event IDs from the agent-wide dedup set and its
            # locator references first; otherwise the re-read would be
            # deduplicated against the stale pre-truncation IDs and silently drop
            # every record whose ID recurs (the typical atomic save-rewrite
            # case), and the locator index would keep dangling pre-truncation
            # offsets.
            if current_size < state.byte_offset_consumed:
                for locator in state.locators:
                    self._existing_event_ids.discard(locator.event_id)
                    self._locator_ref_by_id.pop(locator.event_id, None)
                state.byte_offset_consumed = 0
                state.locators = []

            if current_size == state.byte_offset_consumed and current_mtime == state.last_mtime:
                return []

            try:
                with open(state.file_path, "rb") as f:
                    f.seek(state.byte_offset_consumed)
                    new_data = f.read()
            except OSError as e:
                logger.debug("Failed to read session file {}: {}", state.file_path, e)
                return []

            complete, _fragment = _split_at_last_complete_line(new_data)
            if not complete:
                # Only a partial trailing line so far; leave the offset where it
                # is and re-read on the next poll once the writer flushes.
                return []

            new_events: list[dict[str, Any]] = []
            for byte_offset, byte_len, line_bytes in _iter_line_spans(complete, state.byte_offset_consumed):
                try:
                    decoded_line = line_bytes.decode("utf-8")
                except UnicodeDecodeError as e:
                    logger.warning("UTF-8 decode error in session file {}: {}", state.file_path, e)
                    continue
                line_events = parse_session_lines(
                    decoded_line.splitlines(),
                    existing_event_ids=self._existing_event_ids,
                    tool_name_by_call_id=self._tool_name_by_call_id,
                    session_id=state.session_id,
                )
                for event in line_events:
                    locator = EventLocator(
                        event_id=event["event_id"],
                        timestamp=event.get("timestamp", ""),
                        byte_offset=byte_offset,
                        byte_len=byte_len,
                    )
                    self._locator_ref_by_id[locator.event_id] = (state, len(state.locators))
                    state.locators.append(locator)
                    self._cache_put_locked(event)
                    if event.get("type") == "tool_result" and "subagent_id" in event:
                        self._subagent_id_by_tool_call[event["tool_call_id"]] = event["subagent_id"]
                new_events.extend(line_events)

            state.byte_offset_consumed += len(complete)
            state.last_mtime = current_mtime
            return new_events

    def _enrich_subagent_metadata(self, events: list[dict[str, Any]]) -> None:
        """Enrich Agent tool_use events with subagent metadata.

        Links assistant ``Agent`` tool calls to their subagent sessions and
        attaches the subagent's metadata. The tool_call -> subagent mapping comes
        from the watcher-wide ``_subagent_id_by_tool_call`` index (populated as
        tool_result events are parsed), so enrichment works even when the
        matching tool_result falls outside the returned window. The passed
        events are also scanned so a freshly parsed batch enriches before its
        results are indexed.
        """
        with self._lock:
            subagent_metadata = dict(self._subagent_metadata)
            subagent_by_tool_call = dict(self._subagent_id_by_tool_call)

        for event in events:
            if event.get("type") == "tool_result" and "subagent_id" in event:
                subagent_by_tool_call[event["tool_call_id"]] = event["subagent_id"]

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
                metadata = subagent_metadata.get(sub_id) or subagent_metadata.get(f"agent-{sub_id}")
                if metadata:
                    tc["subagent_metadata"] = metadata

    def _run(self) -> None:
        """Main watcher loop."""
        self._discover_sessions()
        self._setup_watchers()
        self._prime_caches()

        while not self._stop_event.is_set():
            self._wake_event.wait(timeout=_POLL_INTERVAL_SECONDS)
            self._wake_event.clear()

            if self._stop_event.is_set():
                break

            self._discover_sessions()
            self._poll_for_changes()

        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5.0)

    def _prime_caches(self) -> None:
        """Parse the existing backlog into each locator index without emitting it.

        The initial transcript is delivered to clients via the REST tail/backfill
        path, so the watcher must not also broadcast the backlog through
        ``on_events`` (that would flood the bounded SSE queues for long
        histories). Priming builds each file's locator index and advances the
        byte offset to EOF so the poll loop afterwards emits only new events.
        Bodies beyond the cache capacity are dropped here and re-read on demand.
        """
        with self._lock:
            states = list(self._session_states.values())
        for state in states:
            if state.file_path.exists():
                self._ensure_cache_current(state)

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
                    logger.debug("Session file not found for %s, will retry on next cycle", session_id)
                    continue

            with self._lock:
                if session_id in self._session_states:
                    continue
                self._session_states[session_id] = SessionFileState(session_id, file_path)
                self._main_session_ids.append(session_id)

            # Set up watchdog for the new file
            if self._observer is not None:
                parent_dir = str(file_path.parent)
                try:
                    self._observer.schedule(_ChangeHandler(self._wake_event), parent_dir, recursive=False)
                except OSError as e:
                    logger.debug("Failed to schedule watchdog for {}: {}", parent_dir, e)

        # Discover subagent sessions for ALL known sessions (not just newly discovered ones),
        # since subagent files may appear after the parent session is first discovered.
        with self._lock:
            states = list(self._session_states.values())
        for state in states:
            self._discover_subagent_sessions(state.session_id, state.file_path)

    def _discover_subagent_sessions(self, parent_session_id: str, parent_file_path: Path) -> None:
        """Discover subagent session files under <session_id>/subagents/."""
        subagents_dir = parent_file_path.parent / parent_session_id / "subagents"
        if not subagents_dir.exists():
            return

        for jsonl_file in subagents_dir.glob("*.jsonl"):
            sub_id = jsonl_file.stem
            with self._lock:
                if sub_id in self._session_states:
                    continue
                self._session_states[sub_id] = SessionFileState(sub_id, jsonl_file)

            # Read .meta.json for subagent metadata
            meta_file = jsonl_file.with_suffix(".meta.json")
            if meta_file.exists():
                try:
                    meta = json.loads(meta_file.read_text())
                    with self._lock:
                        if sub_id not in self._subagent_metadata:
                            self._subagent_metadata[sub_id] = {
                                "agent_type": meta.get("agentType", ""),
                                "description": meta.get("description", ""),
                                "session_id": sub_id,
                            }
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning("Failed to read subagent metadata {}: {}", meta_file, e)

            if self._observer is not None:
                try:
                    self._observer.schedule(_ChangeHandler(self._wake_event), str(subagents_dir), recursive=False)
                except OSError as e:
                    logger.debug("Failed to schedule watchdog for {}: {}", subagents_dir, e)

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
            handler = _ChangeHandler(self._wake_event)
            for dir_path in watched_dirs:
                observer.schedule(handler, dir_path, recursive=False)
            observer.start()
            self._observer = observer
        except OSError as e:
            logger.debug("Failed to start watchdog observer, falling back to polling only: {}", e)

    def _poll_for_changes(self) -> None:
        """Check all session files for new content and emit newly parsed events."""
        with self._lock:
            states = list(self._session_states.values())

        for state in states:
            if not state.file_path.exists():
                continue

            new_events = self._ensure_cache_current(state)
            if new_events:
                self._enrich_subagent_metadata(new_events)
                self._on_events(self._agent_id, new_events)
