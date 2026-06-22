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

Emission decoupled from parsing
-------------------------------
Both the background poll loop and the synchronous HTTP read paths bring a file's
locator index up to date through the shared ``_ensure_cache_current`` helper. So
the poll loop must not infer "new events to broadcast" from what its own parse
produced -- a concurrent HTTP read may have parsed the tail first. Instead each
``SessionFileState`` tracks an ``emitted_count`` high-water mark over its locator
index, and the poll loop broadcasts every locator past that marker (resolving
their bodies), guaranteeing each event reaches connected SSE clients exactly
once even when an HTTP read was the thread that advanced the byte offset.

Subagent linkage
----------------
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

All access to the shared session collections, per-file locator lists, the body
cache and the subagent linkage maps is guarded by ``_lock`` because the watcher
thread and the Flask request/WebSocket handler threads touch them concurrently. File I/O and parsing
run while the lock is held, but the ``on_events`` callback (which fans out to SSE
queues) is always invoked outside the lock to avoid serializing fan-out and to
avoid re-entrancy / deadlock.
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
from watchdog.observers import Observer

from imbue.system_interface.session_parser import parse_session_lines
from imbue.system_interface.watcher_common import POLL_INTERVAL_SECONDS
from imbue.system_interface.watcher_common import WakeOnChangeHandler

logger = _loguru_logger

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


class SessionFileState:
    """Tracks reading and locator state for a single session JSONL file.

    ``byte_offset_consumed`` is the number of bytes through the last complete
    line that has been parsed into ``locators`` (the append-only locator index
    for the file). ``last_mtime`` lets the poller short-circuit when neither size
    nor mtime changed. Parsed event *bodies* are not stored here -- they live in
    the watcher's bounded body cache.

    ``emitted_count`` is the number of leading ``locators`` already handed to the
    ``on_events`` SSE fan-out. It is tracked separately from parsing so the poll
    loop emits every not-yet-emitted event even when a concurrent HTTP read was
    the thread that actually parsed (and advanced the byte offset past) the new
    tail.
    """

    def __init__(self, session_id: str, file_path: Path) -> None:
        self.session_id = session_id
        self.file_path = file_path
        self.byte_offset_consumed: int = 0
        self.last_mtime: float = 0.0
        self.locators: list[EventLocator] = []
        self.emitted_count: int = 0


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
        # _existing_event_ids, _subagent_metadata, _subagent_meta_read_failed,
        # _subagent_tool_use_id, _subagent_id_by_tool_call,
        # _unlinked_agent_parent_events, _agent_parent_event_ids, _body_cache,
        # _locator_ref_by_id, and every SessionFileState. Held across file I/O and
        # parsing (cheap, incremental, per-agent) but never across the on_events
        # fan-out callback.
        self._lock = threading.Lock()
        self._session_states: dict[str, SessionFileState] = {}
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
        # _rebroadcast_relinked_parents). A parent is dropped only once it is fully
        # *enriched* (every Agent tool_call carries subagent_metadata), not merely
        # once a linkage id exists -- an id can be known (e.g. from the tool_result)
        # a cycle before the subagent's meta.json is discovered, and evicting on bare
        # linkage would drop the parent before the card was ever upgraded.
        self._unlinked_agent_parent_events: dict[str, dict[str, Any]] = {}
        # event_ids of assistant messages that carry an Agent tool_call, recorded only
        # while the priming pass parses the backlog (mark_all_emitted). After priming
        # marks the backlog emitted, this lets _seed_running_agent_parents find parents
        # whose subagent was already in flight when the watcher started (conversation
        # opened mid-run) and make their card upgradeable live -- the poll loop never
        # re-surfaces a primed-emitted event. The seed is the only consumer, so events
        # parsed after priming are not recorded (they reach SSE clients live via the
        # poll loop and would only grow this set without ever being read).
        self._agent_parent_event_ids: set[str] = set()

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

    def get_forward_events(
        self, after_event_id: str, limit: int = 50, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Return up to ``limit`` events immediately AFTER ``after_event_id``.

        The mirror of :meth:`get_backfill_events`, for paging newer when the loaded
        window is not at the live tail (e.g. after a jump to an earlier position).
        Bounded: the cursor is located via ``_locator_ref_by_id`` (O(1)) and at most
        ``limit`` bodies are resolved, so a page costs O(limit).
        """
        if limit <= 0:
            return []
        states = self._selected_states_current(session_id)

        with self._lock:
            page = self._collect_after_locked(states, after_event_id, limit)
            events = self._resolve_bodies_locked(page)

        self._enrich_subagent_metadata(events)
        return events

    def get_events_at_offset(self, offset: int, limit: int, session_id: str | None = None) -> list[dict[str, Any]]:
        """Return up to ``limit`` events starting at global index ``offset``.

        Lets the client jump straight to an arbitrary scroll position (mapped to an
        event index) in a single bounded read, instead of paging through every
        event in between. Bounded: skips whole files by length and resolves at most
        ``limit`` bodies, so it never reads the full transcript. ``offset`` is
        clamped to ``[0, total]``.
        """
        if limit <= 0:
            return []
        states = self._selected_states_current(session_id)

        with self._lock:
            page = self._collect_at_offset_locked(states, max(0, offset), limit)
            events = self._resolve_bodies_locked(page)

        self._enrich_subagent_metadata(events)
        return events

    def get_event_offset(self, event_id: str, session_id: str | None = None) -> int:
        """Global index of ``event_id`` in the selected timeline, or -1 if unknown.

        Each ``/events`` response reports the offset of its first event so the
        client knows where the loaded window sits in the whole conversation -- it
        derives both the scrollbar size and whether more history exists above and
        below from this plus :meth:`get_total_event_count`. O(sessions); resolves
        no bodies.
        """
        states = self._selected_states_current(session_id)
        with self._lock:
            ref = self._locator_ref_by_id.get(event_id)
            if ref is None:
                return -1
            ref_state, ref_idx = ref
            try:
                state_pos = states.index(ref_state)
            except ValueError:
                return -1
            return sum(len(states[pos].locators) for pos in range(state_pos)) + ref_idx

    def get_total_event_count(self, session_id: str | None = None) -> int:
        """Total number of events in the selected timeline.

        Counts locators only -- no body resolution -- so it is O(sessions),
        regardless of transcript length. Lets the client size the scrollbar for
        the whole conversation while only a tail window is loaded, so paging
        older history in does not make the scrollbar jump.
        """
        states = self._selected_states_current(session_id)
        with self._lock:
            return sum(len(state.locators) for state in states)

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

    def _collect_after_locked(
        self, states: list[SessionFileState], after_event_id: str, limit: int
    ) -> list[tuple[SessionFileState, EventLocator]]:
        """Collect up to ``limit`` locators immediately after ``after_event_id`` (lock held).

        Mirror of :meth:`_collect_before_locked`: locates the cursor via
        ``_locator_ref_by_id`` (O(1)) then walks forward across the selected files.
        Returns [] if the cursor is unknown or not in the selected sessions.
        """
        ref = self._locator_ref_by_id.get(after_event_id)
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
        while needed > 0 and pos < len(states):
            state = states[pos]
            start = ref_idx + 1 if state is ref_state else 0
            end = min(len(state.locators), start + needed)
            collected.extend((state, locator) for locator in state.locators[start:end])
            needed -= end - start
            pos += 1
        return collected

    def _collect_at_offset_locked(
        self, states: list[SessionFileState], offset: int, limit: int
    ) -> list[tuple[SessionFileState, EventLocator]]:
        """Collect ``limit`` locators starting at global index ``offset`` (lock held).

        Skips whole files by length until reaching the offset, then takes locators
        across file boundaries -- O(limit + file count), never O(total events).
        """
        collected: list[tuple[SessionFileState, EventLocator]] = []
        skip = offset
        needed = limit
        for state in states:
            count = len(state.locators)
            if skip >= count:
                skip -= count
                continue
            start = skip
            skip = 0
            end = min(count, start + needed)
            collected.extend((state, locator) for locator in state.locators[start:end])
            needed -= end - start
            if needed <= 0:
                break
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

    def _ensure_cache_current(self, state: SessionFileState, mark_all_emitted: bool = False) -> None:
        """Bring ``state``'s locator index up to the file's current contents (lock held internally).

        Appends a locator for each newly parsed event and writes its body into the
        bounded LRU. Emission to SSE clients is decoupled from parsing: callers
        that need to broadcast deltas drive that off ``state.emitted_count`` so a
        concurrent HTTP read parsing the tail does not rob the poll loop of the
        events to emit.

        When ``mark_all_emitted`` is set (the priming path), the whole current
        backlog is marked as already emitted *in the same lock hold* that filled
        the index. Splitting the fill and the mark across two lock acquisitions
        would let a concurrent read append locators in between that then get
        marked emitted and never reach SSE clients.
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
            # Purge this file's event IDs from the agent-wide dedup set and its
            # locator references first; otherwise the re-read would be
            # deduplicated against the stale pre-truncation IDs and silently drop
            # every record whose ID recurs (the typical atomic save-rewrite
            # case), and the locator index would keep dangling pre-truncation
            # offsets. The re-read content must be re-emitted to live SSE clients,
            # so the emission marker resets alongside the index.
            if current_size < state.byte_offset_consumed:
                for locator in state.locators:
                    self._existing_event_ids.discard(locator.event_id)
                    self._locator_ref_by_id.pop(locator.event_id, None)
                state.byte_offset_consumed = 0
                state.locators = []
                state.emitted_count = 0

            if current_size == state.byte_offset_consumed and current_mtime == state.last_mtime:
                if mark_all_emitted:
                    state.emitted_count = len(state.locators)
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
                if mark_all_emitted:
                    state.emitted_count = len(state.locators)
                return

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
                        self._subagent_id_by_tool_call.setdefault(event["tool_call_id"], event["subagent_id"])
                    if (
                        mark_all_emitted
                        and event.get("type") == "assistant_message"
                        and any(tc.get("tool_name") == "Agent" for tc in event.get("tool_calls", []))
                    ):
                        self._agent_parent_event_ids.add(event["event_id"])

            state.byte_offset_consumed += len(complete)
            state.last_mtime = current_mtime
            if mark_all_emitted:
                state.emitted_count = len(state.locators)

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
        """Parse the existing backlog into each locator index without emitting it.

        The initial transcript is delivered to clients via the REST tail/backfill
        path, so the watcher must not also broadcast the backlog through
        ``on_events`` (that would flood the bounded SSE queues for long
        histories). Priming builds each file's locator index, advances the byte
        offset to EOF, and marks the whole backlog as already emitted -- all in a
        single lock hold per file so a concurrent read cannot slip in events that
        then get marked emitted and never reach SSE clients. Bodies beyond the
        cache capacity are dropped here and re-read on demand.
        """
        with self._lock:
            states = list(self._session_states.values())
        for state in states:
            if state.file_path.exists():
                self._ensure_cache_current(state, mark_all_emitted=True)
        self._seed_running_agent_parents()

    def _seed_running_agent_parents(self) -> None:
        """Make backlog Agent parents with a still-running subagent upgradeable live.

        Priming marks the whole backlog as already emitted, so ``_poll_for_changes``
        never re-surfaces a parent that was already on disk when the watcher started --
        the "conversation opened mid-run" case. Without this, a subagent whose linkage
        lands *after* the watcher starts can never upgrade its parent's card live; only
        a page refresh (which re-enriches over the REST path) would show it.

        We seed only parents whose subagent is still running -- at least one Agent
        tool_call has no tool_result yet. Such a subagent's transcript is on disk, so a
        later discovery cycle will populate its metadata and
        ``_rebroadcast_relinked_parents`` will upgrade the card, then drop the parent.
        Finished subagents are skipped: the initial REST render already enriched them
        best-effort, so they need no live upgrade, and seeding them could retain a
        cleaned-up historical parent (no metadata ever coming) in the cache forever.
        """
        with self._lock:
            pairs: list[tuple[SessionFileState, EventLocator]] = []
            for event_id in self._agent_parent_event_ids:
                ref = self._locator_ref_by_id.get(event_id)
                if ref is None:
                    continue
                state, index = ref
                if index < len(state.locators):
                    pairs.append((state, state.locators[index]))
            events = self._resolve_bodies_locked(pairs)
        # Enrich first so parents whose subagent was already discovered at startup are
        # recognized as fully enriched below and are not needlessly cached.
        self._enrich_subagent_metadata(events)
        with self._lock:
            finished_tool_calls = set(self._subagent_id_by_tool_call.keys())
            for event in events:
                agent_tool_calls = [tc for tc in event.get("tool_calls", []) if tc.get("tool_name") == "Agent"]
                if not agent_tool_calls:
                    continue
                if all("subagent_metadata" in tc for tc in agent_tool_calls):
                    continue
                running = any(
                    tc.get("tool_call_id") not in finished_tool_calls
                    for tc in agent_tool_calls
                    if "subagent_metadata" not in tc
                )
                if not running:
                    continue
                message_uuid = event.get("message_uuid", "")
                if message_uuid:
                    self._unlinked_agent_parent_events[message_uuid] = event

    def _discover_sessions(self) -> None:
        """Discover this agent's main sessions and any subagent sessions under them."""
        self._discover_main_sessions_from_history()

        # Discover subagent sessions for ALL known main sessions (not just newly discovered
        # ones), since subagent files may appear after the parent session is first discovered.
        # This must run regardless of whether claude_session_id_history currently exists: a
        # rotated/replaced agent can leave a main session watchable (already in _session_states)
        # while its history file is gone, and subagents that appear after that point still need
        # to be linked. Gating subagent discovery behind the history file -- as it was when this
        # lived after an early return in the history reader -- stranded such subagents' cards on
        # "Running..." forever, even after they finished.
        with self._lock:
            states = list(self._session_states.values())
        for state in states:
            self._discover_subagent_sessions(state.session_id, state.file_path)

    def _discover_main_sessions_from_history(self) -> None:
        """Register any not-yet-known main sessions listed in claude_session_id_history.

        A missing or unreadable history file is a no-op (already-known sessions keep being
        watched); subagent discovery in ``_discover_sessions`` runs either way.
        """
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
                self._main_session_ids.append(session_id)

            # Set up watchdog for the new file
            if self._observer is not None:
                parent_dir = str(file_path.parent)
                try:
                    self._observer.schedule(WakeOnChangeHandler(self._wake_event), parent_dir, recursive=False)
                except OSError as e:
                    logger.debug("Failed to schedule watchdog for {}: {}", parent_dir, e)

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

        Emission is driven by ``emitted_count`` (a high-water mark over the
        locator index) rather than by what this call parsed, so events that a
        concurrent HTTP read parsed into the index are still delivered to
        connected SSE clients exactly once. Pending bodies are resolved from the
        LRU (re-read from disk on a miss) before broadcast.

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
                pending_pairs = [(state, locator) for locator in state.locators[state.emitted_count :]]
                state.emitted_count = len(state.locators)
                pending_events = self._resolve_bodies_locked(pending_pairs)
            if pending_events:
                self._enrich_subagent_metadata(pending_events)
                self._cache_unlinked_agent_parents(pending_events)
                self._on_events(self._agent_id, pending_events)

    def _cache_unlinked_agent_parents(self, events: list[dict[str, Any]]) -> None:
        """Remember assistant messages whose Agent tool_calls aren't enriched yet.

        When an Agent tool_call is broadcast before its subagent's metadata is attached, it
        goes out without subagent_metadata. We keep the event so a later cycle can re-enrich
        and re-broadcast it once the metadata lands (see _rebroadcast_relinked_parents).
        Fully enriched parents are skipped -- there is nothing left to resolve. The check is
        on enrichment, not bare linkage: a tool_call_id can appear in a linkage map (e.g.
        from the tool_result) a cycle before the subagent's meta.json is discovered, and
        skipping it then would lose the card upgrade.
        """
        for event in events:
            if event.get("type") != "assistant_message":
                continue
            agent_tool_calls = [tc for tc in event.get("tool_calls", []) if tc.get("tool_name") == "Agent"]
            if not agent_tool_calls:
                continue
            if self._is_fully_enriched(event):
                continue
            message_uuid = event.get("message_uuid", "")
            if message_uuid:
                with self._lock:
                    self._unlinked_agent_parent_events[message_uuid] = event

    def _rebroadcast_relinked_parents(self) -> None:
        """Re-emit cached parent events that gained subagent links since broadcast.

        A subagent's metadata can appear after the parent Agent tool_call was already
        streamed: the subagent's meta.json (with toolUseId) shows up a cycle later, or its
        tool_result lands later still. Re-enriching the cached parent and re-broadcasting it
        once metadata attaches lets the frontend upgrade the plain tool-call block into the
        rich card without a page refresh. A parent is dropped from the cache only once it is
        fully *enriched* -- every Agent tool_call carries subagent_metadata -- not merely
        once a linkage id exists. The tool_result's agentId can register a tool_call as
        "linked" a cycle before the subagent's meta.json (which holds the card metadata) is
        discovered; dropping on bare linkage there would evict the parent before the card was
        ever upgraded, stranding it on "Running..." until a page refresh.

        The cache snapshot and removals run under ``_lock`` (they mutate shared state), but
        ``_enrich_subagent_metadata`` / ``_is_fully_enriched`` take the lock themselves and
        the ``on_events`` fan-out runs unlocked, so the lock is never held across either.
        """
        with self._lock:
            cached = list(self._unlinked_agent_parent_events.items())

        relinked: list[dict[str, Any]] = []
        fully_enriched_uuids: list[str] = []
        for message_uuid, event in cached:
            before = self._linked_agent_tool_call_ids(event)
            self._enrich_subagent_metadata([event])
            if self._linked_agent_tool_call_ids(event) != before:
                relinked.append(event)
            if self._is_fully_enriched(event):
                fully_enriched_uuids.append(message_uuid)

        if fully_enriched_uuids:
            with self._lock:
                for message_uuid in fully_enriched_uuids:
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

    @staticmethod
    def _is_fully_enriched(event: dict[str, Any]) -> bool:
        """True if every Agent tool_call in ``event`` already carries subagent_metadata.

        This -- not bare linkage -- is the condition for retiring a cached parent: the card
        upgrade the cache exists to deliver is exactly the attachment of subagent_metadata,
        so a parent must stay cached until that has actually happened for all its Agent
        tool_calls. A parent whose subagent transcript is genuinely gone (so metadata can
        never attach) is never cached in the first place: the live path only caches a
        just-spawned subagent's parent, and the priming seed (_seed_running_agent_parents)
        only seeds parents whose subagent is still running -- in both cases the transcript is
        on disk, so enrichment will succeed and the parent will be retired here.
        """
        agent_tool_calls = [tc for tc in event.get("tool_calls", []) if tc.get("tool_name") == "Agent"]
        return bool(agent_tool_calls) and all("subagent_metadata" in tc for tc in agent_tool_calls)
