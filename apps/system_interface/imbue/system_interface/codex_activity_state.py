"""Codex's activity-state derivation.

The codex peer of :mod:`claude_activity_state`. Both consume the common event
schema and the shared primitives in :mod:`activity_state`; the harness dispatch
lives in ``agent_manager._recompute_activity_state``.

Unlike claude, codex writes authoritative turn-boundary markers to its rollout in
real time -- ``task_started`` when a user turn begins, ``task_complete`` /
``turn_aborted`` when it ends (surfaced by ``codex_session_parser`` as
``turn_started`` / ``turn_completed`` / ``turn_aborted`` events). So "the agent is
working" is a simple latch on those, with no reliance on the (unreliable-for-codex)
mngr lifecycle. Verified against real rollouts: ``task_started`` lands ~seconds
before the first assistant text and ``task_complete`` just after the last, so the
dot stays lit across the whole turn and clears only once the text is on screen.
"""

from collections.abc import Sequence
from typing import Any

from imbue.imbue_common.pure import pure
from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.activity_state import is_transcript_tail_stale


@pure
def codex_turn_open(events: Sequence[dict[str, Any]]) -> bool:
    """True iff the codex transcript's most recent turn boundary is an open turn.

    Walks from the end for the latest turn-lifecycle marker: ``turn_started`` -> the
    turn is in progress (True); ``turn_completed`` / ``turn_aborted`` -> the turn
    ended (False). No marker at all -> False (not in a turn). Non-boundary events
    (assistant messages, tool calls/results) are skipped, so a turn that is mid-tool
    still reads open.
    """
    for event in reversed(list(events)):
        event_type = event.get("type")
        if event_type == "turn_started":
            return True
        if event_type in ("turn_completed", "turn_aborted"):
            return False
    return False


@pure
def derive_codex(
    *,
    turn_open: bool,
    has_pending_tool_use: bool,
    tail_event_at: float | None = None,
    process_started_at: float | None = None,
) -> ActivityState:
    """Derive an ``ActivityState`` for a codex agent from the turn latch.

    ``turn_open`` is :func:`codex_turn_open`. ``tail_event_at`` / ``process_started_at``
    feed :func:`activity_state.is_transcript_tail_stale` (using the ``codex_process_started``
    marker) so a turn abandoned by a prior process (a mid-turn restart that left an
    unclosed ``task_started``) reads IDLE rather than pinned "Thinking...".

    Priority:
      1. transcript tail predates the current process (stale) -> IDLE (restart guard).
      2. no open turn -> IDLE (the authoritative waiting-for-the-user signal).
      3. a tool call in flight -> TOOL_RUNNING.
      4. otherwise (turn open, no tool) -> THINKING.
    """
    if is_transcript_tail_stale(tail_event_at=tail_event_at, process_started_at=process_started_at):
        return ActivityState.IDLE
    if not turn_open:
        return ActivityState.IDLE
    if has_pending_tool_use:
        return ActivityState.TOOL_RUNNING
    return ActivityState.THINKING
