"""Per-agent activity state surfaced on the chat panel.

The state is derived from two inputs:
- the agent's mngr lifecycle state (RUNNING, STOPPED, etc.) -- a non-running
  agent is always IDLE regardless of the other signals, which prevents a
  STOPPED agent from appearing as "Thinking..." due to stale transcript data
- the parsed transcript events from the agent's session JSONL files

We deliberately do *not* consult the legacy ``active`` marker file: it can
become stale (e.g. when Claude exits abnormally and the ``Stop`` hook never
runs to clear it), which would falsely show "Thinking..." indefinitely. The
transcript itself is authoritative for IDLE / THINKING / TOOL_RUNNING.
"""

from collections.abc import Sequence
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


RUNNING_LIFECYCLE_STATES: frozenset[str] = frozenset({"RUNNING", "RUNNING_UNKNOWN_AGENT_TYPE"})


@pure
def derive_activity_state(
    *,
    is_agent_running: bool,
    has_pending_tool_use: bool,
    tail_event_type: str | None,
) -> ActivityState:
    """Derive an ``ActivityState`` from lifecycle state and transcript signals.

    ``is_agent_running`` reflects the mngr lifecycle state: ``True`` when the
    agent is in a running state (RUNNING, RUNNING_UNKNOWN_AGENT_TYPE), ``False``
    otherwise (STOPPED, WAITING, REPLACED, DONE, etc.). A non-running agent is
    always IDLE regardless of transcript contents, which prevents a STOPPED agent
    from appearing as "Thinking..." due to stale transcript data.

    ``tail_event_type`` is the cached result of :func:`last_event_type` for the
    agent's current transcript (named distinctly from the helper to avoid
    shadowing it in this scope).

    Priority:
      0. agent not running -> IDLE.
      1. unmatched ``tool_use`` -> TOOL_RUNNING.
      2. last transcript event is ``user_message`` or ``tool_result`` -> THINKING
         (Claude has been handed input but hasn't replied yet).
      3. otherwise (last event is ``assistant_message`` or transcript is empty)
         -> IDLE.
    """
    if not is_agent_running:
        return ActivityState.IDLE
    if has_pending_tool_use:
        return ActivityState.TOOL_RUNNING
    if tail_event_type in ("user_message", "tool_result"):
        return ActivityState.THINKING
    return ActivityState.IDLE
