"""Watch an agent's `.tickets/` directory and expose a step ENRICHMENT table.

The chat progress view derives all *structure* (which steps exist, their
order, their open/close transitions, the grouping of work) from the session
transcript -- the `tk` tool calls and their `Updated <id> -> <status>` output
already live there. tk is the source of *enrichment*, not structure: a
side-table keyed by ticket id holding the canonical title, the close summary,
the current status, and the creation timestamp (used only to order
not-yet-started steps among themselves).

So this watcher maintains a current SNAPSHOT of the agent's step records and:

  - serves it on demand via ``get_enrichment(session_id)`` (read on every GET
    /events and per-subagent events),
  - broadcasts a ``step_enrichment`` message per changed scope whenever the
    snapshot changes, so live subscribers update without a refetch.

The snapshot is keyed by id and joined onto the transcript-derived steps by
id, so it never carries position or ordering information.

Only step records (``step: true``) surface; regular tk tickets are a separate
construct and do not render in the progress view. A step is surfaced to its
creator (the ``agent`` frontmatter stamped by ``tk create``).

A native Claude Code Agent-tool subagent shares the parent's ``.tickets/`` dir
and agent name, so its step records are indistinguishable from the main
agent's by the files alone. To keep a subagent's steps out of the main
progress view (and to give the subagent's own conversation a real timeline),
the snapshot is *scoped per session*: this watcher takes an attribution
provider (backed by the session watcher, the only place session identity
exists) and splits its step records into the main view's steps and each
subagent session's steps. ``get_enrichment(session_id)`` serves one scope;
changes broadcast one ``step_enrichment`` message per changed scope -- the main
scope untagged, each subagent scope tagged with its ``session_id`` so the
existing per-stream SSE filters route it to the right conversation. When no
attribution provider is supplied, every step falls in the main scope
(the original single-agent behaviour). See ``step_attribution.py``.
"""

from __future__ import annotations

import threading
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Callable

from loguru import logger as _loguru_logger
from watchdog.observers import Observer

from imbue.system_interface.step_attribution import StepAttribution
from imbue.system_interface.step_attribution import attribute_steps
from imbue.system_interface.tickets_parser import TicketState
from imbue.system_interface.tickets_parser import parse_ticket_file
from imbue.system_interface.watcher_common import POLL_INTERVAL_SECONDS
from imbue.system_interface.watcher_common import WakeOnChangeHandler

logger = _loguru_logger

# The SSE message type carrying a full enrichment snapshot. The frontend
# replaces its enrichment table for the message's scope whenever one arrives.
ENRICHMENT_MESSAGE_TYPE = "step_enrichment"

# Internal scope key for the main view's steps (as opposed to a subagent
# session id). A snapshot under this key is served on the main /events path and
# broadcast as an *untagged* step_enrichment message, which the existing SSE
# routing delivers to the main stream only.
_MAIN_SCOPE: str | None = None

# Fixed-width microsecond UTC ISO-8601, e.g. 2026-04-28T01:00:00.123456Z, so a
# plain lexicographic compare equals chronological order -- the invariant the
# frontend relies on when ordering pending steps by `created_at`. tk writes
# this format; older tickets and file mtimes are normalised up to it here.
_ISO_MICROS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def _mtime_iso(mtime: float) -> str:
    """File mtime as a fixed-width microsecond UTC ISO-8601 timestamp. Used as
    the `created_at` fallback when a ticket lacks the frontmatter field."""
    return datetime.fromtimestamp(mtime, tz=timezone.utc).strftime(_ISO_MICROS_FORMAT)


def _normalize_iso(ts: str) -> str | None:
    """Reformat an ISO-8601 UTC timestamp to fixed-width microsecond precision.
    Returns None if the value can't be parsed, so the caller can fall back to
    the file mtime instead of feeding a malformed string into the snapshot --
    where it would silently break the lexicographic `created_at` sort the
    frontend relies on to order pending steps."""
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        logger.debug("Malformed `created` timestamp {!r}; falling back to file mtime", ts)
        return None
    if parsed.tzinfo is None:
        # A timestamp with no offset is assumed UTC (tk stamps `Z`); otherwise
        # astimezone() would read it as machine-local time and shift it.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime(_ISO_MICROS_FORMAT)


class AgentTicketsWatcher:
    """Watches an agent's `.tickets/` directory and maintains a step
    enrichment snapshot.

    The directory is allowed to not exist yet; the watcher stays silent
    (empty snapshot) until it appears. Once present, it observes changes
    (watchdog + polling fallback) and broadcasts a fresh snapshot on change.
    """

    def __init__(
        self,
        agent_id: str,
        agent_name: str,
        tickets_dir: Path,
        on_events: Callable[[str, list[dict[str, Any]]], None],
        attribution_provider: Callable[[], StepAttribution] | None = None,
    ) -> None:
        self._agent_id = agent_id
        # Used to filter step records to this agent: a step surfaces only to
        # the agent stamped as its `agent` (creator) by `tk create`. A step
        # with no creator stamp (older tk) surfaces to everyone.
        self._agent_name = agent_name
        self._tickets_dir = tickets_dir
        self._on_events = on_events
        # Supplies the transcript-derived `id -> session_id` attribution used to
        # scope steps per session. None disables scoping: every step falls in
        # the main scope (single-agent behaviour, and the path tests without a
        # session watcher take).
        self._attribution_provider = attribution_provider

        # Current snapshot, scoped by session: scope -> {ticket_id -> {title,
        # summary, status, created_at}}. The `_MAIN_SCOPE` (None) key holds the
        # main view's steps; each other key is a subagent session id. Joined
        # onto the transcript-derived steps by id on the frontend.
        self._scoped_enrichment: dict[str | None, dict[str, dict[str, Any]]] = {_MAIN_SCOPE: {}}
        # Serialises _scan / get_enrichment across the _run thread and request
        # handler threads so the snapshot is never read mid-rebuild.
        self._scan_lock = threading.Lock()

        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._observer: Any = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"tickets-watcher-{self._agent_id}")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._observer is not None:
            self._observer.stop()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def get_enrichment(self, session_id: str | None = None) -> dict[str, dict[str, Any]]:
        """Return the current step enrichment snapshot for one scope
        (ticket_id -> {title, summary, status, created_at}).

        ``session_id=None`` returns the main view's steps; a subagent session id
        returns that subagent's steps. Refreshes from disk first (so a GET
        /events always reflects the latest ticket state), and broadcasts any
        change to live subscribers -- mirroring how the session watcher forwards
        request-time discoveries so other websocket subscribers don't miss them.
        """
        _changed, _old, new = self._refresh_and_broadcast()
        return self._copy_scope(new.get(session_id, {}))

    def _run(self) -> None:
        self._setup_watchers()
        while not self._stop_event.is_set():
            self._wake_event.wait(timeout=POLL_INTERVAL_SECONDS)
            self._wake_event.clear()
            if self._stop_event.is_set():
                break
            self._refresh_and_broadcast()

        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5.0)

    def _refresh_and_broadcast(
        self,
    ) -> tuple[bool, dict[str | None, dict[str, dict[str, Any]]], dict[str | None, dict[str, dict[str, Any]]]]:
        """Rescan, broadcast a step_enrichment message per changed scope, and
        return ``(changed, old_scoped, new_scoped)`` (both copies)."""
        changed, old, new = self._scan_and_copy()
        if changed:
            messages = self._diff_messages(old, new)
            if messages:
                self._on_events(self._agent_id, messages)
        return changed, old, new

    def _setup_watchers(self) -> None:
        # Watch the parent of .tickets/ (non-recursively) so we get a wake-up
        # if the .tickets/ directory itself is created mid-run. If it already
        # exists, also watch it for sub-second latency on ticket changes.
        parent_dir = self._tickets_dir.parent
        if not parent_dir.exists():
            logger.debug("Tickets parent dir {} does not exist; running in poll-only mode", parent_dir)
            return
        try:
            observer = Observer()
            handler = WakeOnChangeHandler(self._wake_event)
            observer.schedule(handler, str(parent_dir), recursive=False)
            if self._tickets_dir.exists():
                observer.schedule(handler, str(self._tickets_dir), recursive=False)
            observer.start()
            self._observer = observer
        except OSError as e:
            logger.warning("Failed to start watchdog observer for tickets dir {}: {}", parent_dir, e)

    def _scan_and_copy(
        self,
    ) -> tuple[bool, dict[str | None, dict[str, dict[str, Any]]], dict[str | None, dict[str, dict[str, Any]]]]:
        """Rebuild the scoped snapshot from disk and copy it, atomically under a
        single `_scan_lock` acquisition, returning (changed, old_copy, new_copy).

        Detection and copy MUST happen in the same critical section: doing them
        in two separate locked sections lets a concurrent scan (the poll thread
        or another request handler) slip between them and advance the snapshot,
        decoupling the copy a caller broadcasts from the change it detected.
        """
        with self._scan_lock:
            old = self._copy_scoped()
            changed = self._rebuild_locked()
            new = self._copy_scoped()
            return changed, old, new

    def _rebuild_locked(self) -> bool:
        """Rebuild `_scoped_enrichment` from disk in place; return True if it
        changed. Re-reads every step ticket each scan (ticket directories are
        small), then scopes the result per session via the attribution provider.
        The caller MUST hold `_scan_lock`.
        """
        flat, step_records = self._read_step_records()
        scoped = self._scope_enrichment(flat, step_records)
        if scoped == self._scoped_enrichment:
            return False
        self._scoped_enrichment = scoped
        return True

    def _read_step_records(self) -> tuple[dict[str, dict[str, Any]], list[tuple[str, str, str]]]:
        """Read this agent's step tickets from disk into a flat enrichment table
        (ticket_id -> entry) and a parallel list of `(ticket_id, title, status)`
        records (the input the attribution join needs)."""
        flat: dict[str, dict[str, Any]] = {}
        step_records: list[tuple[str, str, str]] = []
        if not self._tickets_dir.exists():
            return flat, step_records

        for md_file in sorted(self._tickets_dir.glob("*.md")):
            try:
                stat = md_file.stat()
            except OSError as e:
                logger.debug("Skipping ticket file {}: {}", md_file, e)
                continue

            state = parse_ticket_file(md_file)
            if state is None:
                continue
            # Only step records render; regular tickets are dropped.
            if not state.step:
                continue
            if self._should_skip_for_agent(state):
                continue

            # Fall back to the file mtime when `created` is absent *or* malformed
            # -- either way the snapshot needs a sortable timestamp, never a raw
            # unparseable string.
            created_at = _mtime_iso(stat.st_mtime)
            if state.created_at:
                normalized = _normalize_iso(state.created_at)
                if normalized is not None:
                    created_at = normalized
            flat[state.ticket_id] = {
                "title": state.title,
                "summary": state.summary if state.status == "closed" else None,
                "status": state.status,
                "created_at": created_at,
            }
            step_records.append((state.ticket_id, state.title, state.status))

        return flat, step_records

    def _scope_enrichment(
        self,
        flat: dict[str, dict[str, Any]],
        step_records: list[tuple[str, str, str]],
    ) -> dict[str | None, dict[str, dict[str, Any]]]:
        """Split the flat step table into per-session scopes using the
        attribution provider. Without a provider (or with no steps), every step
        stays in the main scope -- the original single-agent behaviour."""
        if self._attribution_provider is None or not flat:
            return {_MAIN_SCOPE: flat}

        attribution = self._attribution_provider()
        owner_by_id = attribute_steps(step_records, attribution)
        main_sessions = set(attribution.main_session_ids)

        scoped: dict[str | None, dict[str, dict[str, Any]]] = {_MAIN_SCOPE: {}}
        for ticket_id, entry in flat.items():
            owner = owner_by_id.get(ticket_id)
            # Unknown owner defaults to the main view -- the safe place for a
            # step we could not attribute (it shows somewhere rather than
            # vanishing).
            if owner is None or owner in main_sessions:
                scoped[_MAIN_SCOPE][ticket_id] = entry
            else:
                scoped.setdefault(owner, {})[ticket_id] = entry
        return scoped

    def _should_skip_for_agent(self, state: TicketState) -> bool:
        """A step record surfaces only to its creator (the `agent` stamp set
        by `tk create` from $MNGR_AGENT_NAME). A step with no creator stamp
        (older tk) surfaces to everyone, so a rollout doesn't make historical
        steps vanish."""
        return bool(state.agent) and state.agent != self._agent_name

    def _copy_scoped(self) -> dict[str | None, dict[str, dict[str, Any]]]:
        """Deep-enough copy of the scoped snapshot for handing to foreign code."""
        return {scope: self._copy_scope(table) for scope, table in self._scoped_enrichment.items()}

    def _copy_scope(self, table: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Deep-enough copy of one scope's enrichment table."""
        return {ticket_id: dict(entry) for ticket_id, entry in table.items()}

    def _diff_messages(
        self,
        old: dict[str | None, dict[str, dict[str, Any]]],
        new: dict[str | None, dict[str, dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        """One step_enrichment message per scope whose snapshot changed. The
        main scope is broadcast untagged; each subagent scope is tagged with its
        session id so the existing SSE filters route it to that conversation. A
        scope that emptied out is broadcast as an empty snapshot, so its
        subscribers clear their stale table."""
        messages: list[dict[str, Any]] = []
        if old.get(_MAIN_SCOPE) != new.get(_MAIN_SCOPE):
            messages.append(self._enrichment_message(new.get(_MAIN_SCOPE, {}), _MAIN_SCOPE))
        subagent_scopes = (set(old) | set(new)) - {_MAIN_SCOPE}
        for session_id in sorted(subagent_scopes):
            if old.get(session_id) != new.get(session_id):
                messages.append(self._enrichment_message(new.get(session_id, {}), session_id))
        return messages

    def _enrichment_message(self, snapshot: dict[str, dict[str, Any]], session_id: str | None) -> dict[str, Any]:
        """The SSE message carrying a full enrichment snapshot for one scope.
        Tagged with `session_id` for a subagent scope; untagged for the main
        scope so `is_main_session_event` keeps it on the main stream."""
        message: dict[str, Any] = {"type": ENRICHMENT_MESSAGE_TYPE, "enrichment": snapshot}
        if session_id is not None:
            message["session_id"] = session_id
        return message
