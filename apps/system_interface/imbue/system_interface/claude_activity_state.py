"""Claude's activity-state derivation.

The claude peer of :mod:`codex_activity_state`. Both consume the common event
schema and the shared primitives in :mod:`activity_state`; the harness dispatch
lives in ``agent_manager._recompute_activity_state``.

Claude has no turn-boundary events in its transcript, so "the agent is working"
is inferred from the mngr lifecycle (RUNNING) plus the transcript tail: an
unmatched ``tool_use`` means a tool is in flight, and a tail of ``user_message``
/ ``tool_result`` means claude has been handed input but has not replied yet.
"""

from imbue.imbue_common.pure import pure
from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.activity_state import is_transcript_tail_stale


@pure
def derive_claude(
    *,
    is_agent_running: bool,
    has_pending_tool_use: bool,
    tail_event_type: str | None,
    tail_event_at: float | None = None,
    process_started_at: float | None = None,
) -> ActivityState:
    """Derive an ``ActivityState`` for a claude agent from lifecycle + transcript signals.

    ``is_agent_running`` reflects the mngr lifecycle (RUNNING / RUNNING_UNKNOWN_AGENT_TYPE);
    a non-running agent is always IDLE, which prevents a STOPPED agent from appearing
    as "Thinking..." on stale transcript data. ``tail_event_type`` is the cached
    :func:`activity_state.last_event_type`; ``tail_event_at`` / ``process_started_at``
    feed :func:`activity_state.is_transcript_tail_stale` to drop a turn abandoned by a
    prior process (a mid-turn restart).

    Priority:
      0. agent not running -> IDLE.
      1. transcript tail predates the current process (stale) -> IDLE.
      2. unmatched ``tool_use`` -> TOOL_RUNNING.
      3. last transcript event is ``user_message`` or ``tool_result`` -> THINKING.
      4. otherwise (last event is ``assistant_message`` or empty) -> IDLE.
    """
    if not is_agent_running:
        return ActivityState.IDLE
    if is_transcript_tail_stale(tail_event_at=tail_event_at, process_started_at=process_started_at):
        return ActivityState.IDLE
    if has_pending_tool_use:
        return ActivityState.TOOL_RUNNING
    if tail_event_type in ("user_message", "tool_result"):
        return ActivityState.THINKING
    return ActivityState.IDLE
