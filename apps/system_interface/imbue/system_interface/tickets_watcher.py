"""Watch an agent's `.tickets/` directory and expose a step ENRICHMENT table.

The chat progress view derives all *structure* (which steps exist, their
order, their open/close transitions, the grouping of work) from the session
transcript -- the `tk` tool calls and their `Updated <id> -> <status>` output
already live there. tk is the source of *enrichment*, not structure: a
side-table keyed by ticket id holding the canonical title, the close summary,
the current status, and the creation timestamp (used only to order
not-yet-started steps among themselves).

So this watcher maintains a current SNAPSHOT of the agent's step records and:

  - serves it on demand via ``get_enrichment()`` (read on every GET /events),
  - broadcasts a single ``step_enrichment`` message whenever the snapshot
    changes, so live subscribers update without a refetch.

The snapshot is keyed by id and joined onto the transcript-derived steps by
id, so it never carries position or ordering information.

Only step records (``step: true``) surface; regular tk tickets are a separate
construct and do not render in the progress view. A step is surfaced to its
creator (the ``agent`` frontmatter stamped by ``tk create``).
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

# The SSE message type carrying a full enrichment snapshot. The frontend
# replaces its per-agent enrichment table whenever one of these arrives.
ENRICHMENT_MESSAGE_TYPE = "step_enrichment"

# Fixed-width microsecond UTC ISO-8601, e.g. 2026-04-28T01:00:00.123456Z, so a
# plain lexicographic compare equals chronological order -- the invariant the
# frontend relies on when ordering pending steps by `created_at`. tk writes
# this format; older tickets and file mtimes are normalised up to it here.
_ISO_MICROS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def _mtime_iso(mtime: float) -> str:
    """File mtime as a fixed-width microsecond UTC ISO-8601 timestamp. Used as
    the `created_at` fallback when a ticket lacks the frontmatter field."""
    return datetime.fromtimestamp(mtime, tz=timezone.utc).strftime(_ISO_MICROS_FORMAT)


def _normalize_iso(ts: str) -> str:
    """Reformat an ISO-8601 UTC timestamp to fixed-width microsecond precision.
    Returns the input unchanged if it can't be parsed, so a malformed field
    never crashes the scan."""
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts
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
    ) -> None:
        self._agent_id = agent_id
        # Used to filter step records to this agent: a step surfaces only to
        # the agent stamped as its `agent` (creator) by `tk create`. A step
        # with no creator stamp (older tk) surfaces to everyone.
        self._agent_name = agent_name
        self._tickets_dir = tickets_dir
        self._on_events = on_events

        # Current snapshot: ticket_id -> {title, summary, status, created_at}.
        self._enrichment: dict[str, dict[str, Any]] = {}
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

    def get_enrichment(self) -> dict[str, dict[str, Any]]:
        """Return the current step enrichment snapshot for this agent
        (ticket_id -> {title, summary, status, created_at}).

        Refreshes from disk first (so a GET /events always reflects the
        latest ticket state), and broadcasts the change to live subscribers
        if the refresh found one -- mirroring how the session watcher forwards
        request-time discoveries so other websocket subscribers don't miss
        them.
        """
        changed, snapshot = self._scan_and_copy()
        if changed:
            self._on_events(self._agent_id, [self._enrichment_message(snapshot)])
        return snapshot

    def _run(self) -> None:
        self._setup_watchers()
        while not self._stop_event.is_set():
            self._wake_event.wait(timeout=POLL_INTERVAL_SECONDS)
            self._wake_event.clear()
            if self._stop_event.is_set():
                break
            changed, snapshot = self._scan_and_copy()
            if changed:
                self._on_events(self._agent_id, [self._enrichment_message(snapshot)])

        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5.0)

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

    def _scan_and_copy(self) -> tuple[bool, dict[str, dict[str, Any]]]:
        """Rebuild the snapshot from disk and copy it, atomically under a single
        `_scan_lock` acquisition, returning (changed, snapshot_copy).

        Detection and copy MUST happen in the same critical section: doing them
        in two separate locked sections lets a concurrent scan (the poll thread
        or another request handler) slip between them and advance the snapshot,
        decoupling the copy a caller broadcasts from the change it detected.
        """
        with self._scan_lock:
            changed = self._rebuild_locked()
            return changed, self._copy_enrichment()

    def _rebuild_locked(self) -> bool:
        """Rebuild `_enrichment` from disk in place; return True if it changed.
        Re-reads every step ticket each scan (ticket directories are small).
        The caller MUST hold `_scan_lock`.
        """
        if not self._tickets_dir.exists():
            if self._enrichment:
                self._enrichment = {}
                return True
            return False

        new_enrichment: dict[str, dict[str, Any]] = {}
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

            created_at = _normalize_iso(state.created_at) if state.created_at else _mtime_iso(stat.st_mtime)
            new_enrichment[state.ticket_id] = {
                "title": state.title,
                "summary": state.summary if state.status == "closed" else None,
                "status": state.status,
                "created_at": created_at,
            }

        if new_enrichment == self._enrichment:
            return False
        self._enrichment = new_enrichment
        return True

    def _should_skip_for_agent(self, state: TicketState) -> bool:
        """A step record surfaces only to its creator (the `agent` stamp set
        by `tk create` from $MNGR_AGENT_NAME). A step with no creator stamp
        (older tk) surfaces to everyone, so a rollout doesn't make historical
        steps vanish."""
        return bool(state.agent) and state.agent != self._agent_name

    def _copy_enrichment(self) -> dict[str, dict[str, Any]]:
        """Deep-enough copy of the snapshot for handing to foreign code."""
        return {ticket_id: dict(entry) for ticket_id, entry in self._enrichment.items()}

    def _enrichment_message(self, snapshot: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """The SSE message carrying a full enrichment snapshot."""
        return {"type": ENRICHMENT_MESSAGE_TYPE, "enrichment": snapshot}
