"""Watch an agent's `.tickets/` directory for tk ticket changes.

Mirrors the pattern of session_watcher.AgentSessionWatcher: a background
thread combines watchdog filesystem events with mtime-based polling, and
emits parsed events through an `on_events(agent_id, events)` callback.

Each ticket file produces one event per state TRANSITION the watcher
actually observes. Stable event ids of `<ticket_id>-<status>` let the
frontend dedup across watcher restarts.

Live (watcher running through the whole ticket lifecycle): emits one
event per status change, so a typical ticket produces three events
(open at creation -> in_progress at `tk start` -> closed at `tk close`).

Replay (watcher started against a directory that already has tickets at
some non-`open` status): we cannot recover the historical timestamps of
transitions that already happened, so we emit a SINGLE event for the
current status with the file's mtime. The `created_at` field on the
event carries the ticket's frontmatter `created` value, so the frontend
still knows when the ticket existed even if it never saw the `open`
event. We do NOT synthesize fake `open` / `in_progress` events on
replay -- the event stream stays a faithful description of what was
observed, with the frontend filling in any missing lifecycle fields.
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

from imbue.system_interface.tickets_parser import TicketState
from imbue.system_interface.tickets_parser import parse_ticket_file
from imbue.system_interface.watcher_common import POLL_INTERVAL_SECONDS
from imbue.system_interface.watcher_common import WakeOnChangeHandler

logger = _loguru_logger

_SOURCE = "tk"


# Fixed-width microsecond UTC ISO-8601, e.g. 2026-04-28T01:00:00.123456Z. Every
# timestamp the watcher emits is normalised to this single format so that a
# plain lexicographic comparison equals chronological order -- the invariant
# the frontend's step ordering and window attribution rely on. (tk writes this
# same format; older tickets and file mtimes get normalised up to it here.)
_ISO_MICROS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def _mtime_iso(mtime: float) -> str:
    """File mtime formatted as a fixed-width microsecond UTC ISO-8601 timestamp.
    Caller passes the already-obtained `stat_result.st_mtime` so we don't
    re-stat (and risk an OSError if the file was deleted between calls). Used as
    the fallback transition timestamp when a ticket lacks the corresponding
    frontmatter field (older tk, or a transition tk didn't stamp)."""
    return datetime.fromtimestamp(mtime, tz=timezone.utc).strftime(_ISO_MICROS_FORMAT)


def _normalize_iso(ts: str) -> str:
    """Reformat an ISO-8601 UTC timestamp to fixed-width microsecond precision.
    Accepts second- or sub-second-resolution input (tk's `created` / `started`
    / `closed` fields, which an older tk wrote at second resolution). Returns
    the input unchanged if it can't be parsed -- an unparseable value is left
    as-is rather than dropped, so a malformed field never crashes the scan."""
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts
    return parsed.astimezone(timezone.utc).strftime(_ISO_MICROS_FORMAT)


class AgentTicketsWatcher:
    """Watches an agent's `.tickets/` directory and emits task events.

    The directory is allowed to not exist yet; the watcher just stays
    silent until it's created. Once present, it observes changes
    (watchdog + polling fallback) and emits events for status changes.
    """

    def __init__(
        self,
        agent_id: str,
        agent_name: str,
        tickets_dir: Path,
        on_events: Callable[[str, list[dict[str, Any]]], None],
    ) -> None:
        self._agent_id = agent_id
        # Used to filter tickets into this agent's progress stream. The
        # filter is two-rule (see _scan): step records surface to their
        # CREATOR (the `agent:` frontmatter field stamped by `tk create`
        # from $MNGR_AGENT_NAME), and regular tickets surface to their
        # CURRENT ASSIGNEE (the `assignee:` field, set by `tk start`
        # auto-self-assignment or `tk assign`). Pre-stamping tickets with
        # neither field set fall back to "show to anyone" for backwards
        # compatibility.
        self._agent_name = agent_name
        self._tickets_dir = tickets_dir
        self._on_events = on_events

        self._last_status_per_ticket: dict[str, str] = {}
        self._mtime_cache: dict[str, tuple[float, int]] = {}
        # Cumulative log of every event _scan() has emitted. Returned by
        # get_all_events() so consumers (e.g. /events on page reload) see
        # the full history -- _scan() itself is incremental and only
        # surfaces transitions observed since the last scan.
        self._emitted_events: list[dict[str, Any]] = []
        # Serialises _scan / get_all_events. The watcher's _run() thread
        # and FastAPI request handler threads (via get_all_events) both
        # mutate _last_status_per_ticket / _mtime_cache / _emitted_events;
        # without this lock concurrent _scan() calls can double-emit the
        # same transition.
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

    def get_all_events(self) -> list[dict[str, Any]]:
        """Return every event observed by this watcher so far -- the full
        cumulative history of state transitions, in timestamp order.

        Idempotent across calls: an unchanged tickets directory yields
        the same list every time. Used to seed the chat view's initial
        state on each GET /events (matching the contract of
        AgentSessionWatcher.get_all_events()), so multiple page reloads
        keep returning the full history.

        Any transitions discovered by THIS call's catch-up scan are also
        forwarded through `_on_events`. Without that, a request-handler
        scan would mark transitions as "seen" in the watcher's internal
        cache before the background `_run` loop got a chance to observe
        them -- so other connected websocket subscribers for the same
        agent would silently miss those events until they too refetched.
        """
        # _scan() returns exactly the events IT appended on this call
        # (under _scan_lock), so it is the canonical "what did this call
        # newly emit" answer regardless of any concurrent _run() scans
        # that interleave before or after. The snapshot is taken under
        # the lock purely for the return value; broadcast happens outside
        # the lock since _on_events is foreign code (today it just
        # enqueues to a thread-safe queue, but we don't want to hold our
        # lock across arbitrary callbacks).
        new_events = self._scan()
        with self._scan_lock:
            snapshot = list(self._emitted_events)
        if new_events:
            self._on_events(self._agent_id, new_events)
        return snapshot

    def _run(self) -> None:
        self._setup_watchers()
        while not self._stop_event.is_set():
            self._wake_event.wait(timeout=POLL_INTERVAL_SECONDS)
            self._wake_event.clear()
            if self._stop_event.is_set():
                break

            new_events = self._scan()
            if new_events:
                self._on_events(self._agent_id, new_events)

        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5.0)

    def _setup_watchers(self) -> None:
        # Watch the parent of .tickets/ (non-recursively) so we get a
        # wake-up if the .tickets/ directory itself is created mid-run.
        # If .tickets/ already exists, also watch it (non-recursively)
        # for sub-second latency on ticket file changes. recursive=True
        # on a project root would fire on every unrelated file change in
        # the agent's work tree, which is wasteful and can hit inotify
        # watch limits on busy checkouts.
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
            # Watchdog start failure (typically inotify watch limit reached or
            # a permissions issue). Log at warning so operators see the
            # degradation; the polling-only fallback in _run keeps the
            # watcher functional, just with higher latency.
            logger.warning("Failed to start watchdog observer for tickets dir {}: {}", parent_dir, e)

    def _scan(self) -> list[dict[str, Any]]:
        """Scan the tickets directory and emit one event per OBSERVED
        state change. On first sighting of a new ticket, emit one event
        for its current status; on subsequent scans, emit one event each
        time the status moves forward (open -> in_progress -> closed).
        We never synthesize transitions we didn't observe.

        Holds _scan_lock for the entirety of the scan so a concurrent
        get_all_events() call from a request handler thread cannot
        race with the background _run() loop and double-emit the same
        transition.
        """
        with self._scan_lock:
            if not self._tickets_dir.exists():
                return []

            new_events: list[dict[str, Any]] = []
            for md_file in sorted(self._tickets_dir.glob("*.md")):
                try:
                    stat = md_file.stat()
                except OSError as e:
                    # Most commonly the file was deleted between glob() and
                    # stat() -- benign -- but permission errors etc. would
                    # also land here. Log at debug so the skip is traceable
                    # without spamming production logs.
                    logger.debug("Skipping ticket file {}: {}", md_file, e)
                    continue

                mtime_key = (stat.st_mtime, stat.st_size)
                cached = self._mtime_cache.get(md_file.name)
                if cached == mtime_key:
                    continue
                self._mtime_cache[md_file.name] = mtime_key

                state = parse_ticket_file(md_file)
                if state is None:
                    continue

                # Per-agent surfacing rule. Step records (turn-bound
                # progress markers) are creator-private -- they only
                # surface to the agent that created them. Regular
                # tickets surface to their CURRENT assignee, so a ticket
                # one agent picks up via `tk start <id>` (auto-self-assign)
                # appears in the picker's progress view rather than the
                # originator's. Backwards-compat: when both `step` and
                # `assignee` are absent (pre-existing pre-stamping
                # tickets), we keep the legacy "any agent sees it"
                # behaviour so the rollout doesn't make historical
                # tickets vanish from the chat.
                if self._should_skip_for_agent(state):
                    continue

                previous_status = self._last_status_per_ticket.get(state.ticket_id)
                if previous_status == state.status:
                    continue

                self._last_status_per_ticket[state.ticket_id] = state.status

                # Timestamp the transition from the ticket file itself -- the
                # source of truth -- using the frontmatter field that matches
                # the current status: `created` for open, `started` for
                # in_progress, `closed` for closed. tk stamps all three, so
                # this is truthful even on replay (a ticket discovered already
                # in_progress reports when work actually began, not when the
                # watcher first saw the file). Fall back to the file's mtime
                # when the field is absent (older tk, or a transition tk didn't
                # stamp) -- never to an empty string, which would sort to the
                # front of the merged stream and break turn attribution. All
                # values are normalised to one fixed-width microsecond format.
                ts = self._transition_timestamp(state, stat.st_mtime)

                new_events.append(self._make_event(state, ts))

            new_events.sort(key=lambda e: e["timestamp"])
            # Accumulate so get_all_events() can replay the full history on
            # subsequent calls (e.g. page reloads).
            self._emitted_events.extend(new_events)
            return new_events

    def _should_skip_for_agent(self, state: TicketState) -> bool:
        """Per-agent surfacing rule, factored out so the if/elif chain
        reads as a series of independent early-return cases rather than
        a single conditional cascade. Returns True iff this watcher
        instance (for `self._agent_name`) should drop the given ticket.

        - Step records (creator-private): surface only to the agent
          stamped as `agent:` (`MNGR_AGENT_NAME` at create time). Skip
          when the stamp is set to a different agent.
        - Regular tickets WITH an assignee: surface to the assignee.
          Skip when the assignee is someone else.
        - Regular tickets WITHOUT an assignee but WITH a creator stamp:
          surface to the creator (pre-pickup state -- filed but not yet
          picked up by anyone).
        - Everything else (no step, no assignee, no agent stamp -- the
          pre-stamping legacy shape): surface to every agent. Keeps
          rollout from making historical tickets vanish.
        """
        if state.step:
            return bool(state.agent) and state.agent != self._agent_name
        if state.assignee:
            return state.assignee != self._agent_name
        if state.agent:
            return state.agent != self._agent_name
        return False

    def _transition_timestamp(self, state: TicketState, mtime: float) -> str:
        """The timestamp for the event emitted for `state`'s current status,
        taken from the matching frontmatter field and normalised to the
        fixed-width microsecond format. Falls back to the file's mtime when the
        field is missing (older tk / unstamped transition)."""
        field_by_status = {
            "open": state.created_at,
            "in_progress": state.started_at,
            "closed": state.closed_at,
        }
        raw = field_by_status.get(state.status, "")
        return _normalize_iso(raw) if raw else _mtime_iso(mtime)

    def _make_event(self, state: TicketState, ts: str) -> dict[str, Any]:
        summary_at = state.summary_at if state.status == "closed" else None
        return {
            "type": "task_event",
            "event_id": f"{state.ticket_id}-{state.status}",
            "timestamp": ts,
            "source": _SOURCE,
            "ticket_id": state.ticket_id,
            "title": state.title,
            "status": state.status,
            # created_at and summary_at are normalised to the same fixed-width
            # microsecond format as `timestamp` so every timestamp the frontend
            # receives compares consistently. created_at falls back to the
            # event timestamp when the frontmatter `created` field is empty (a
            # malformed ticket) so it is never an empty string.
            "created_at": _normalize_iso(state.created_at) if state.created_at else ts,
            "summary": state.summary if state.status == "closed" else None,
            "summary_at": _normalize_iso(summary_at) if summary_at else summary_at,
            # Step / parent_id / assignee thread through to the frontend
            # so turn-grouping can a) group step children under their
            # parent ticket and b) attribute regular tickets to the turn
            # in which their current assignee first acted on them.
            "step": state.step,
            "parent_id": state.parent_id,
            "assignee": state.assignee,
        }
