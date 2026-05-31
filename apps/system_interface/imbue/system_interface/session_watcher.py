"""Watch raw Claude session JSONL files for new events.

Uses watchdog for low-latency filesystem change detection with mtime-based
polling as a safety net fallback, following the pattern from watcher_common.py
in mngr_recursive.
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


def _read_subagent_parent_info(jsonl_file: Path) -> dict[str, str] | None:
    """Read the first line of a subagent jsonl to recover its parent linkage.

    Returns a dict with `parent_assistant_uuid`, `agent_id`, and `first_timestamp`,
    or None if the file is unreadable, empty, malformed, or missing the linkage
    fields (in which case the caller should retry on the next discovery cycle).
    """
    try:
        with open(jsonl_file, "rb") as f:
            first_line_bytes = f.readline()
    except OSError as exc:
        logger.debug("Failed to read subagent jsonl first line {}: {}", jsonl_file, exc)
        return None
    if not first_line_bytes.strip():
        return None
    # If the line has no trailing newline, the writer is mid-write. Silently
    # return so the caller retries on the next cycle, rather than spamming a
    # warning every poll until the line is complete.
    if not first_line_bytes.endswith(b"\n"):
        return None
    try:
        first = json.loads(first_line_bytes)
    except json.JSONDecodeError:
        logger.warning("Subagent jsonl first line is not valid JSON: {}", jsonl_file)
        return None
    parent_uuid = first.get("sourceToolAssistantUUID")
    if not isinstance(parent_uuid, str) or not parent_uuid:
        # Older Claude Code session schemas wrote only `parentUuid`; on newer
        # versions it appears to mirror sourceToolAssistantUUID for sidechain
        # sessions, so this is a safe fallback.
        parent_uuid = first.get("parentUuid")
    if not isinstance(parent_uuid, str) or not parent_uuid:
        return None
    agent_id = first.get("agentId")
    if not isinstance(agent_id, str) or not agent_id:
        return None
    timestamp = first.get("timestamp", "")
    if not isinstance(timestamp, str):
        timestamp = ""
    return {
        "parent_assistant_uuid": parent_uuid,
        "agent_id": agent_id,
        "first_timestamp": timestamp,
    }


class _ChangeHandler(FileSystemEventHandler):
    """Watchdog handler that wakes the watcher on actual file changes."""

    def __init__(self, wake_event: threading.Event) -> None:
        self._wake_event = wake_event

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type in _NON_CHANGE_EVENT_TYPES:
            return
        self._wake_event.set()


class SessionFileState:
    """Tracks reading state for a single session JSONL file."""

    def __init__(self, session_id: str, file_path: Path) -> None:
        self.session_id = session_id
        self.file_path = file_path
        self.byte_offset: int = 0
        self.last_mtime: float = 0.0
        self.last_size: int = 0


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

        self._session_states: dict[str, SessionFileState] = {}
        self._known_session_ids: list[str] = []
        self._main_session_ids: list[str] = []
        self._tool_name_by_call_id: dict[str, str] = {}
        self._existing_event_ids: set[str] = set()
        self._subagent_metadata: dict[str, dict[str, str]] = {}  # sub_id -> {agent_type, description}
        # sub_ids whose meta.json we've already determined is permanently malformed.
        # Used to log the warning once per file instead of once per poll cycle.
        self._subagent_meta_read_failed: set[str] = set()
        # sub_id -> {parent_assistant_uuid, agent_id, first_timestamp} read from the subagent
        # jsonl's first line. Lets us link a parent Agent tool_use to its subagent the moment
        # the subagent starts writing, before any tool_result lands.
        self._subagent_parent_info: dict[str, dict[str, str]] = {}
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
        self._mtime_cache: dict[str, tuple[float, int]] = {}

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
        all_events: list[dict[str, Any]] = []

        for state in self._session_states.values():
            if not state.file_path.exists():
                continue

            # Filter by session if requested
            if session_id is not None and state.session_id != session_id:
                continue
            # Default: only main sessions
            if session_id is None and state.session_id not in self._main_session_ids:
                continue

            try:
                content = state.file_path.read_text()
                lines = content.splitlines()
            except OSError:
                logger.debug("Failed to read session file: %s", state.file_path)
                continue

            tool_names: dict[str, str] = {}
            events = parse_session_lines(
                lines,
                existing_event_ids=None,
                tool_name_by_call_id=tool_names,
                session_id=state.session_id,
            )
            self._tool_name_by_call_id.update(tool_names)
            for event in events:
                self._existing_event_ids.add(event["event_id"])
            all_events.extend(events)

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
        return session_id in self._main_session_ids

    def _enrich_subagent_metadata(self, events: list[dict[str, Any]]) -> None:
        """Enrich Agent tool_use events with subagent metadata.

        Builds a tool_call_id -> subagent_id map from two sources, in order:

        1. Disk-based: each subagent jsonl's first line carries
           `sourceToolAssistantUUID` pointing at the parent assistant message
           UUID, and its own `agentId`. Subagents sharing a parent assistant
           UUID are paired with that message's Agent tool_uses by order
           (subagents written first → tool_uses listed first). This works the
           moment a subagent starts, before its tool_result lands, so running
           subagents get the rich card.

        2. Fallback: tool_result events that already carry a `subagent_id`
           (extracted from the structured `toolUseResult.agentId` field or the
           legacy `agentId:` text trailer). Used for sessions whose subagent
           jsonls are no longer on disk.
        """
        subagent_by_tool_call: dict[str, str] = {}

        # 1. Disk-based linkage: group cached subagent parent info by parent assistant UUID,
        #    ordered by first-line timestamp so multiple Agent tool_uses in a single
        #    assistant message line up with their subagents in spawn order.
        subagents_by_parent: dict[str, list[tuple[str, str]]] = {}
        for info in self._subagent_parent_info.values():
            parent_uuid = info["parent_assistant_uuid"]
            agent_id = info["agent_id"]
            timestamp = info["first_timestamp"]
            subagents_by_parent.setdefault(parent_uuid, []).append((timestamp, agent_id))
        for items in subagents_by_parent.values():
            items.sort()

        for event in events:
            if event.get("type") != "assistant_message":
                continue
            parent_uuid = event.get("message_uuid", "")
            sub_pairs = subagents_by_parent.get(parent_uuid, [])
            if not sub_pairs:
                continue
            agent_tool_calls = [tc for tc in event.get("tool_calls", []) if tc.get("tool_name") == "Agent"]
            for tc, (_ts, agent_id) in zip(agent_tool_calls, sub_pairs, strict=False):
                subagent_by_tool_call[tc["tool_call_id"]] = agent_id

        # 2. Fallback to tool_result-based linkage for tool_use_ids not resolved above.
        for event in events:
            if event.get("type") != "tool_result":
                continue
            if "subagent_id" not in event:
                continue
            tool_call_id = event["tool_call_id"]
            if tool_call_id not in subagent_by_tool_call:
                subagent_by_tool_call[tool_call_id] = event["subagent_id"]

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
        self._read_initial_offsets()

        while not self._stop_event.is_set():
            self._wake_event.wait(timeout=_POLL_INTERVAL_SECONDS)
            self._wake_event.clear()

            if self._stop_event.is_set():
                break

            self._discover_sessions()
            self._rebroadcast_relinked_parents()
            self._poll_for_changes()

        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5.0)

    def _discover_sessions(self) -> None:
        """Read claude_session_id_history to find all session IDs."""
        history_file = self._agent_state_dir / "claude_session_id_history"
        if not history_file.exists():
            return

        try:
            lines = history_file.read_text().splitlines()
        except OSError:
            return

        for line in lines:
            parts = line.strip().split()
            if not parts:
                continue
            session_id = parts[0]
            if session_id in self._session_states:
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

            self._session_states[session_id] = SessionFileState(session_id, file_path)
            self._known_session_ids.append(session_id)
            self._main_session_ids.append(session_id)

            # Set up watchdog for the new file
            if self._observer is not None:
                parent_dir = str(file_path.parent)
                try:
                    self._observer.schedule(_ChangeHandler(self._wake_event), parent_dir, recursive=False)
                except OSError:
                    logger.debug("Failed to schedule watchdog for %s", parent_dir)

        # Discover subagent sessions for ALL known sessions (not just newly discovered ones),
        # since subagent files may appear after the parent session is first discovered.
        for state in list(self._session_states.values()):
            self._discover_subagent_sessions(state.session_id, state.file_path)

    def _discover_subagent_sessions(self, parent_session_id: str, parent_file_path: Path) -> None:
        """Discover subagent session files under <session_id>/subagents/."""
        subagents_dir = parent_file_path.parent / parent_session_id / "subagents"
        if not subagents_dir.exists():
            return

        for jsonl_file in subagents_dir.glob("*.jsonl"):
            sub_id = jsonl_file.stem

            if sub_id not in self._session_states:
                self._session_states[sub_id] = SessionFileState(sub_id, jsonl_file)
                self._known_session_ids.append(sub_id)
                if self._observer is not None:
                    try:
                        self._observer.schedule(_ChangeHandler(self._wake_event), str(subagents_dir), recursive=False)
                    except OSError:
                        pass

            # Cache .meta.json. Retry on each pass while the read fails with OSError
            # (transient: mid-write, momentary permission glitch). Give up after a
            # JSONDecodeError (truly malformed -- won't self-heal) so we don't spam
            # the log on every poll cycle.
            if sub_id not in self._subagent_metadata and sub_id not in self._subagent_meta_read_failed:
                meta_file = jsonl_file.with_suffix(".meta.json")
                if meta_file.exists():
                    try:
                        meta = json.loads(meta_file.read_text())
                        self._subagent_metadata[sub_id] = {
                            "agent_type": meta.get("agentType", ""),
                            "description": meta.get("description", ""),
                            "session_id": sub_id,
                        }
                    except json.JSONDecodeError as exc:
                        logger.warning("Subagent meta.json is not valid JSON, giving up: {}: {}", meta_file, exc)
                        self._subagent_meta_read_failed.add(sub_id)
                    except OSError as exc:
                        logger.debug("Failed to read subagent meta.json {}: {}", meta_file, exc)

            # Cache parent linkage from the subagent jsonl's first line. The first line is
            # written when the subagent starts -- before any tool_result -- so this makes
            # rich card rendering work for running subagents too. Retry on each pass until
            # the first line is present and parseable.
            if sub_id not in self._subagent_parent_info:
                parent_info = _read_subagent_parent_info(jsonl_file)
                if parent_info is not None:
                    self._subagent_parent_info[sub_id] = parent_info

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
        for state in self._session_states.values():
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
        except OSError:
            logger.debug("Failed to start watchdog observer, falling back to polling only")

    def _read_initial_offsets(self) -> None:
        """Set byte offsets to end of file so we only get new events from the watcher.

        The initial load is handled separately by get_all_events().
        """
        for state in self._session_states.values():
            if state.file_path.exists():
                try:
                    stat = state.file_path.stat()
                    state.byte_offset = stat.st_size
                    state.last_mtime = stat.st_mtime
                    state.last_size = stat.st_size
                except OSError:
                    pass

    def _poll_for_changes(self) -> None:
        """Check all session files for new content."""
        for state in self._session_states.values():
            if not state.file_path.exists():
                continue

            try:
                stat = state.file_path.stat()
            except OSError:
                continue

            # mtime/size check -- skip if unchanged
            current_mtime = stat.st_mtime
            current_size = stat.st_size
            if current_mtime == state.last_mtime and current_size == state.last_size:
                continue

            state.last_mtime = current_mtime
            state.last_size = current_size

            if current_size <= state.byte_offset:
                continue

            # Read new bytes
            try:
                with open(state.file_path, "rb") as f:
                    f.seek(state.byte_offset)
                    new_data = f.read()
                state.byte_offset = state.byte_offset + len(new_data)
            except OSError:
                continue

            new_lines = new_data.decode("utf-8", errors="replace").splitlines()
            if not new_lines:
                continue

            new_events = parse_session_lines(
                new_lines,
                existing_event_ids=self._existing_event_ids,
                tool_name_by_call_id=self._tool_name_by_call_id,
                session_id=state.session_id,
            )

            if new_events:
                self._enrich_subagent_metadata(new_events)
                self._cache_unlinked_agent_parents(new_events)
                self._on_events(self._agent_id, new_events)

    def _cache_unlinked_agent_parents(self, events: list[dict[str, Any]]) -> None:
        """Remember assistant messages whose Agent tool_calls are not yet linked.

        When an Agent tool_call is broadcast before its subagent's jsonl exists, it
        goes out without subagent_metadata. We keep the event so a later discovery
        cycle can re-enrich and re-broadcast it (see _rebroadcast_relinked_parents).
        Fully-linked parents are skipped -- there is nothing left to resolve.
        """
        for event in events:
            if event.get("type") != "assistant_message":
                continue
            agent_tool_calls = [tc for tc in event.get("tool_calls", []) if tc.get("tool_name") == "Agent"]
            if not agent_tool_calls:
                continue
            if all("subagent_metadata" in tc for tc in agent_tool_calls):
                continue
            message_uuid = event.get("message_uuid", "")
            if message_uuid:
                self._unlinked_agent_parent_events[message_uuid] = event

    def _rebroadcast_relinked_parents(self) -> None:
        """Re-emit cached parent events that gained subagent links since broadcast.

        A subagent's jsonl (and thus its parent linkage) can appear after the parent
        Agent tool_call was already streamed to the frontend. Re-enriching the cached
        parent and re-broadcasting it lets the frontend upgrade the plain tool-call
        block into the rich subagent card without a page refresh. Parents whose Agent
        tool_calls are now all linked are dropped from the cache so memory stays
        bounded and they are not re-checked every cycle.
        """
        relinked: list[dict[str, Any]] = []
        for message_uuid, event in list(self._unlinked_agent_parent_events.items()):
            before = self._linked_agent_tool_call_ids(event)
            self._enrich_subagent_metadata([event])
            if self._linked_agent_tool_call_ids(event) != before:
                relinked.append(event)
            if self._is_fully_linked(event):
                del self._unlinked_agent_parent_events[message_uuid]
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
    def _is_fully_linked(event: dict[str, Any]) -> bool:
        """True if every Agent tool_call in ``event`` has subagent_metadata."""
        agent_tool_calls = [tc for tc in event.get("tool_calls", []) if tc.get("tool_name") == "Agent"]
        return bool(agent_tool_calls) and all("subagent_metadata" in tc for tc in agent_tool_calls)
