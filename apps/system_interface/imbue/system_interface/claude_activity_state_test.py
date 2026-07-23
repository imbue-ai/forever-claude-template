import pytest

from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.activity_state import RUNNING_LIFECYCLE_STATES
from imbue.system_interface.claude_activity_state import derive_claude


@pytest.mark.parametrize(
    "has_pending_tool_use, tail_event_type, expected",
    [
        pytest.param(True, "assistant_message", ActivityState.TOOL_RUNNING, id="tool_running_when_unmatched_tool"),
        pytest.param(False, "user_message", ActivityState.THINKING, id="thinking_when_tail_user_message"),
        pytest.param(False, "tool_result", ActivityState.THINKING, id="thinking_when_tail_tool_result"),
        pytest.param(False, "assistant_message", ActivityState.IDLE, id="idle_when_tail_assistant_message"),
        pytest.param(False, None, ActivityState.IDLE, id="idle_when_no_events"),
    ],
)
def test_derive_claude(has_pending_tool_use: bool, tail_event_type: str | None, expected: ActivityState) -> None:
    state = derive_claude(
        is_agent_running=True,
        has_pending_tool_use=has_pending_tool_use,
        tail_event_type=tail_event_type,
    )
    assert state == expected


@pytest.mark.parametrize(
    "lifecycle_state",
    [
        pytest.param("STOPPED", id="stopped"),
        pytest.param("WAITING", id="waiting"),
        pytest.param("REPLACED", id="replaced"),
        pytest.param("DONE", id="done"),
    ],
)
def test_derive_claude_non_running_agent_is_always_idle(lifecycle_state: str) -> None:
    assert lifecycle_state not in RUNNING_LIFECYCLE_STATES
    state = derive_claude(is_agent_running=False, has_pending_tool_use=True, tail_event_type="user_message")
    assert state == ActivityState.IDLE


@pytest.mark.parametrize(
    "has_pending_tool_use, tail_event_type",
    [
        pytest.param(True, "assistant_message", id="would_be_tool_running"),
        pytest.param(False, "tool_result", id="would_be_thinking"),
        pytest.param(False, "user_message", id="would_be_thinking_user_message"),
    ],
)
def test_derive_claude_stale_tail_overrides_to_idle(has_pending_tool_use: bool, tail_event_type: str) -> None:
    """A tail older than the current process (a turn abandoned by a prior process) reads IDLE."""
    state = derive_claude(
        is_agent_running=True,
        has_pending_tool_use=has_pending_tool_use,
        tail_event_type=tail_event_type,
        tail_event_at=100.0,
        process_started_at=200.0,
    )
    assert state == ActivityState.IDLE


def test_derive_claude_fresh_tail_still_reports_working() -> None:
    """A tail written after the current process started drives the normal state."""
    state = derive_claude(
        is_agent_running=True,
        has_pending_tool_use=True,
        tail_event_type="assistant_message",
        tail_event_at=300.0,
        process_started_at=200.0,
    )
    assert state == ActivityState.TOOL_RUNNING
