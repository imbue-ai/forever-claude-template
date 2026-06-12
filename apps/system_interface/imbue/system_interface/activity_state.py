"""Per-agent activity state surfaced on the chat panel.

The state is derived from three inputs:
- the agent's mngr lifecycle state (RUNNING, STOPPED, etc.) -- a non-running
  agent is always IDLE regardless of the other signals, which prevents a
  STOPPED agent from appearing as "Thinking..." due to stale transcript data
- the parsed transcript events from the agent's session JSONL files
- the mtime of the ``claude_process_started`` marker (touched by mngr on every
  startup/resume) -- a transcript tail older than the current process is left
  over from a turn this process never ran, so it must not show "Thinking..."

We deliberately do *not* consult the legacy ``active`` marker file: it can
become stale (e.g. when Claude exits abnormally and the ``Stop`` hook never
runs to clear it), which would falsely show "Thinking..." indefinitely. The
transcript is authoritative for IDLE / THINKING / TOOL_RUNNING *within* the
current process; the ``claude_process_started`` boundary guards against a
transcript abandoned mid-turn by a prior process (e.g. a container restart),
which the transcript alone cannot distinguish from work still in flight.
"""

from collections.abc import Sequence
from datetime import datetime
from enum import auto
from pathlib import Path
from typing import Any

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.pure import pure

# Touched by mngr in the agent's state dir on every Claude startup/resume (a
# fresh, not-mid-turn process). Its mtime is the restart boundary that the
# staleness fences below compare transcript timestamps against.
CLAUDE_PROCESS_STARTED_MARKER = "claude_process_started"


def read_process_started_at(agent_state_dir: Path) -> float | None:
    """Return the mtime of the agent's ``claude_process_started`` marker, or None.

    The single accessor for the mngr marker contract (see
    ``CLAUDE_PROCESS_STARTED_MARKER``); returns ``None`` when the marker does not
    exist yet (e.g. an agent created before mngr started writing it), in which
    case the staleness fences no-op.
    """
    marker = agent_state_dir / CLAUDE_PROCESS_STARTED_MARKER
    try:
        return marker.stat().st_mtime
    except OSError:
        return None


class ActivityState(UpperCaseStrEnum):
    """The activity state of a chat agent, as surfaced above the message input."""

    IDLE = auto()
    THINKING = auto()
    TOOL_RUNNING = auto()


@pure
def has_unmatched_tool_use(events: Sequence[dict[str, Any]]) -> bool:
    """True iff the transcript has at least one ``tool_use`` without a matching ``tool_result``.

    Walks every event so that an unmatched ``tool_use`` from any prior assistant
    turn still counts -- in practice Claude only ever has outstanding tool calls
    from its most recent assistant message, but the matching is order-independent
    so we don't have to care.
    """
    pending: set[str] = set()
    matched: set[str] = set()
    for event in events:
        event_type = event.get("type")
        if event_type == "assistant_message":
            for tool_call in event.get("tool_calls") or ():
                tool_call_id = tool_call.get("tool_call_id")
                if tool_call_id:
                    pending.add(tool_call_id)
        elif event_type == "tool_result":
            tool_call_id = event.get("tool_call_id")
            if tool_call_id:
                matched.add(tool_call_id)
        else:
            # user_message or other event types we don't care about for tool tracking.
            pass
    return bool(pending - matched)


@pure
def newest_unmatched_tool_use_timestamp(events: Sequence[dict[str, Any]]) -> str | None:
    """ISO-8601 timestamp of the newest assistant message holding an unmatched ``tool_use``.

    Returns ``None`` when every tool_use is matched, or when the newest pending
    assistant message carries no usable timestamp. Feeds
    :func:`is_pending_tool_use_stale` (see there): an unmatched tool_use is only
    evidence of a *currently running* tool if the current Claude process issued
    it. A tool_use abandoned by an interrupt/restart never gets a tool_result --
    not even a later resume writes one -- so without the fence it would pin the
    state at TOOL_RUNNING for the rest of the transcript's life.

    The newest pending message is the right fence target: while a fresh turn has
    a tool in flight, that newer tool_use governs (TOOL_RUNNING is correct); once
    it resolves, only the abandoned one remains and the fence retires it.
    """
    matched: set[str] = set()
    for event in events:
        if event.get("type") == "tool_result":
            tool_call_id = event.get("tool_call_id")
            if tool_call_id:
                matched.add(tool_call_id)
    newest: str | None = None
    for event in events:
        if event.get("type") != "assistant_message":
            continue
        has_unmatched = any(
            tool_call.get("tool_call_id") and tool_call.get("tool_call_id") not in matched
            for tool_call in event.get("tool_calls") or ()
        )
        if has_unmatched:
            timestamp = event.get("timestamp")
            newest = timestamp if isinstance(timestamp, str) and timestamp else None
    return newest


@pure
def last_event_type(events: Sequence[dict[str, Any]]) -> str | None:
    """Return the ``type`` of the final transcript event, or ``None`` if empty."""
    if not events:
        return None
    return events[-1].get("type")


@pure
def last_event_timestamp(events: Sequence[dict[str, Any]]) -> str | None:
    """Return the ISO-8601 ``timestamp`` of the final transcript event, or ``None``.

    Events are ordered by timestamp, so the final event's timestamp is the most
    recent transcript activity. Returns ``None`` for an empty transcript or a
    final event without a timestamp (e.g. an injected non-transcript event).
    """
    if not events:
        return None
    timestamp = events[-1].get("timestamp")
    return timestamp if isinstance(timestamp, str) and timestamp else None


@pure
def parse_iso_timestamp_to_epoch(timestamp: str | None) -> float | None:
    """Parse an ISO-8601 transcript timestamp (e.g. ``2026-06-08T19:42:15.191Z``)
    into epoch seconds, or ``None`` if it is missing or unparseable.

    Claude writes UTC timestamps with a trailing ``Z``; ``fromisoformat`` accepts
    that on the Python versions this app targets. The result is an absolute epoch,
    directly comparable to a filesystem mtime regardless of timezone.
    """
    if not timestamp:
        return None
    try:
        return datetime.fromisoformat(timestamp).timestamp()
    except ValueError:
        return None


RUNNING_LIFECYCLE_STATES: frozenset[str] = frozenset({"RUNNING", "RUNNING_UNKNOWN_AGENT_TYPE"})


@pure
def is_transcript_tail_stale(
    *,
    tail_event_at: float | None,
    process_started_at: float | None,
) -> bool:
    """True iff the newest transcript event predates the current Claude process.

    ``tail_event_at`` is the epoch time of the final transcript event;
    ``process_started_at`` is the mtime of the agent's ``claude_process_started``
    marker, which mngr touches on every startup/resume (a fresh, not-mid-turn
    process). When the newest event is older than that boundary, it belongs to a
    turn the *current* process never ran -- e.g. a turn abandoned mid-flight when
    a container restart killed Claude. Its "still working" tail (an unmatched
    ``tool_use`` or a trailing ``tool_result``) would otherwise pin the indicator
    at TOOL_RUNNING / THINKING forever, since the dead turn will never emit the
    closing ``assistant_message`` that settles it back to IDLE.

    Returns ``False`` when either input is missing (no marker yet, or a final
    event without a timestamp): we only override on positive evidence of
    staleness, otherwise the transcript signals stand.
    """
    if tail_event_at is None or process_started_at is None:
        return False
    return tail_event_at < process_started_at


@pure
def is_pending_tool_use_stale(
    *,
    pending_tool_use_at: float | None,
    process_started_at: float | None,
) -> bool:
    """True iff the newest unmatched ``tool_use`` predates the current Claude process.

    ``pending_tool_use_at`` is the epoch time of the newest assistant message with
    an unmatched tool_use (see :func:`newest_unmatched_tool_use_timestamp`);
    ``process_started_at`` is the ``claude_process_started`` marker mtime. A tool
    call issued before the current process started cannot still be running -- the
    process that issued it is dead. This covers a turn abandoned mid-tool by an
    interrupt or restart, whose tool_use never receives a tool_result: unlike the
    stale-*tail* guard, it keeps working after new turns land on the transcript.

    Returns ``False`` when either input is missing: we only override on positive
    evidence of staleness, otherwise the pending-tool signal stands.
    """
    if pending_tool_use_at is None or process_started_at is None:
        return False
    return pending_tool_use_at < process_started_at


@pure
def derive_activity_state(
    *,
    is_agent_running: bool,
    has_pending_tool_use: bool,
    tail_event_type: str | None,
    tail_event_at: float | None = None,
    process_started_at: float | None = None,
    pending_tool_use_at: float | None = None,
) -> ActivityState:
    """Derive an ``ActivityState`` from lifecycle state and transcript signals.

    ``is_agent_running`` reflects the mngr lifecycle state: ``True`` when the
    agent is in a running state (RUNNING, RUNNING_UNKNOWN_AGENT_TYPE), ``False``
    otherwise (STOPPED, WAITING, REPLACED, DONE, etc.). A non-running agent is
    always IDLE regardless of transcript contents, which prevents a STOPPED agent
    from appearing as "Thinking..." due to stale transcript data.

    ``tail_event_type`` is the cached result of :func:`last_event_type` for the
    agent's current transcript (named distinctly from the helper to avoid
    shadowing it in this scope). ``tail_event_at`` and ``process_started_at`` feed
    :func:`is_transcript_tail_stale` (see there): together they detect a tail left
    over from before the current process started, which a running-but-idle agent
    would otherwise show as "Thinking..." indefinitely after a mid-turn restart.
    ``pending_tool_use_at`` and ``process_started_at`` feed
    :func:`is_pending_tool_use_stale` (see there): they retire an unmatched
    ``tool_use`` abandoned by an interrupt/restart, which -- unlike a stale tail --
    would otherwise pin TOOL_RUNNING even after new turns land.

    Priority:
      0. agent not running -> IDLE.
      1. transcript tail predates the current process (stale) -> IDLE.
      2. unmatched ``tool_use``, unless it predates the current process
         -> TOOL_RUNNING.
      3. last transcript event is ``user_message`` or ``tool_result`` -> THINKING
         (Claude has been handed input but hasn't replied yet).
      4. otherwise (last event is ``assistant_message`` or transcript is empty)
         -> IDLE.
    """
    if not is_agent_running:
        return ActivityState.IDLE
    if is_transcript_tail_stale(tail_event_at=tail_event_at, process_started_at=process_started_at):
        return ActivityState.IDLE
    if has_pending_tool_use and not is_pending_tool_use_stale(
        pending_tool_use_at=pending_tool_use_at, process_started_at=process_started_at
    ):
        return ActivityState.TOOL_RUNNING
    if tail_event_type in ("user_message", "tool_result"):
        return ActivityState.THINKING
    return ActivityState.IDLE
