"""Shared primitives for per-agent activity state surfaced on the chat panel.

Holds the common building blocks both harnesses use: the ``ActivityState`` enum,
the transcript walkers (``has_unmatched_tool_use``, ``last_event_type``,
``last_event_timestamp``), the timestamp parser, and the ``is_transcript_tail_stale``
restart guard. The actual IDLE / THINKING / TOOL_RUNNING *derivation* lives in the
two harness peers -- :mod:`claude_activity_state` (lifecycle + transcript tail) and
:mod:`codex_activity_state` (the ``task_started`` / ``task_complete`` turn latch) --
and the harness dispatch is in ``agent_manager._recompute_activity_state``.

The ``*_process_started`` marker (touched by mngr on every startup/resume) is the
boundary the stale-tail guard compares against: a transcript tail older than the
current process is left over from a turn this process never ran and must not show
"Thinking..." indefinitely after a mid-turn restart.
"""

from collections.abc import Sequence
from datetime import datetime
from enum import auto
from typing import Any

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.pure import pure


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
