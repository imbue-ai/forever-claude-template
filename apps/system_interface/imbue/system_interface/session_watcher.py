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

All access to the shared session collections and per-file caches is guarded by
``_lock`` because the watcher thread and FastAPI handler threads touch them
concurrently. File I/O and parsing run while the lock is held, but the
``on_events`` callback (which fans out to SSE queues) is always invoked outside
the lock to avoid serializing fan-out and to avoid re-entrancy.
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
from watchdog.events import FileSystemEvent
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from imbue.system_interface.session_parser import parse_session_lines

logger = _loguru_logger

_NON_CHANGE_EVENT_TYPES = frozenset({"opened", "closed", "closed_no_write"})

_POLL_INTERVAL_SECONDS = 1.0
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


class _ChangeHandler(FileSystemEventHandler):
    """Watchdog handler that wakes the watcher on actual file changes."""

    def __init__(self, wake_event: threading.Event) -> None:
        self._wake_event = wake_event

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type in _NON_CHANGE_EVENT_TYPES:
            return
        self._wake_event.set()


class SessionFileState:
    """Tracks reading and parsed-events cache state for a single session JSONL file.

    ``byte_offset_consumed`` is the number of bytes through the last complete
    line that has been parsed into ``events`` (an append-only cache of all
    parsed events for the file). ``last_mtime`` lets the poller short-circuit
    when neither size nor mtime changed.
    """

    def __init__(self, session_id: str, file_path: Path) -> None:
        self.session_id = session_id
        self.file_path = file_path
        self.byte_offset_consumed: int = 0
        self.last_mtime: float = 0.0
        self.events: list[dict[str, Any]] = []


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

        # Guards _session_states, _main_session_ids, _tool_name_by_call_id,
        # _existing_event_ids, _subagent_metadata, and every SessionFileState.
        # Held across file I/O and parsing (cheap, incremental, per-agent) but
        # never across the on_events fan-out callback.
        self._lock = threading.Lock()
        self._session_states: dict[str, SessionFileState] = {}
        self._main_session_ids: list[str] = []
        self._tool_name_by_call_id: dict[str, str] = {}
        self._existing_event_ids: set[str] = set()
        self._subagent_metadata: dict[str, dict[str, str]] = {}  # sub_id -> {agent_type, description}

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

    def _ensure_cache_current(self, state: SessionFileState) -> list[dict[str, Any]]:
        """Bring ``state``'s cache up to the file's current contents under the lock.

        Returns the events newly parsed by this call (empty if nothing changed),
        so a caller acting as the live source -- the poll loop -- can emit only
        the genuinely new events. The full accumulated transcript always lives in
        ``state.events``.
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
            # Discard this file's event IDs from the agent-wide dedup set first;
            # otherwise the re-read would be deduplicated against the stale
            # pre-truncation IDs and silently drop every record whose ID recurs
            # (the typical atomic save-rewrite case), leaving the cache empty.
            if current_size < state.byte_offset_consumed:
                for event in state.events:
                    self._existing_event_ids.discard(event["event_id"])
                state.byte_offset_consumed = 0
                state.events = []

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

            try:
                decoded = complete.decode("utf-8")
            except UnicodeDecodeError as e:
                logger.warning("UTF-8 decode error in session file {}: {}", state.file_path, e)
                return []

            new_events = parse_session_lines(
                decoded.splitlines(),
                existing_event_ids=self._existing_event_ids,
                tool_name_by_call_id=self._tool_name_by_call_id,
                session_id=state.session_id,
            )
            state.byte_offset_consumed += len(complete)
            state.last_mtime = current_mtime
            state.events.extend(new_events)
            return new_events

    def _enrich_subagent_metadata(self, events: list[dict[str, Any]]) -> None:
        """Enrich Agent tool_use events with subagent metadata.

        Matches tool_result events that have a subagent_id (extracted from
        Agent tool results) to their corresponding tool_use events, and adds
        subagent_metadata to the assistant_message that contains the tool_use.
        """
        with self._lock:
            subagent_metadata = dict(self._subagent_metadata)

        # Build map: tool_call_id -> subagent_id from tool_result events
        subagent_by_tool_call: dict[str, str] = {}
        for event in events:
            if event.get("type") == "tool_result" and "subagent_id" in event:
                subagent_by_tool_call[event["tool_call_id"]] = event["subagent_id"]

        # Enrich assistant messages that have Agent tool calls
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
        """Parse the existing backlog into each cache without emitting it.

        The initial transcript is delivered to clients via the REST
        ``get_all_events`` path, so the watcher must not also broadcast the
        backlog through ``on_events`` (that would flood the bounded SSE queues
        for long histories). Priming fills ``cache.events`` and advances the
        byte offset to EOF so the poll loop afterwards emits only new events.
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
