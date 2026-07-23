from typing import Any

import pytest

from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.codex_activity_state import codex_turn_open
from imbue.system_interface.codex_activity_state import derive_codex


@pytest.mark.parametrize(
    "events, expected",
    [
        pytest.param([], False, id="empty_is_not_open"),
        pytest.param([{"type": "turn_started"}], True, id="started_is_open"),
        pytest.param([{"type": "turn_started"}, {"type": "turn_completed"}], False, id="completed_is_closed"),
        pytest.param([{"type": "turn_started"}, {"type": "turn_aborted"}], False, id="aborted_is_closed"),
        # A turn mid-flight: started, then non-boundary events -> still open.
        pytest.param(
            [{"type": "turn_started"}, {"type": "assistant_message"}, {"type": "tool_result"}],
            True,
            id="mid_turn_still_open",
        ),
        # A second turn started after a completed one -> open again.
        pytest.param(
            [{"type": "turn_started"}, {"type": "turn_completed"}, {"type": "turn_started"}],
            True,
            id="new_turn_reopens",
        ),
        pytest.param([{"type": "assistant_message"}], False, id="no_markers_is_not_open"),
    ],
)
def test_codex_turn_open(events: list[dict[str, Any]], expected: bool) -> None:
    assert codex_turn_open(events) is expected


@pytest.mark.parametrize(
    "turn_open, has_pending_tool_use, expected",
    [
        pytest.param(False, False, ActivityState.IDLE, id="closed_is_idle"),
        pytest.param(False, True, ActivityState.IDLE, id="closed_is_idle_even_with_dangling_tool"),
        pytest.param(True, False, ActivityState.THINKING, id="open_no_tool_is_thinking"),
        pytest.param(True, True, ActivityState.TOOL_RUNNING, id="open_with_tool_is_running"),
    ],
)
def test_derive_codex(turn_open: bool, has_pending_tool_use: bool, expected: ActivityState) -> None:
    assert derive_codex(turn_open=turn_open, has_pending_tool_use=has_pending_tool_use) == expected


def test_derive_codex_stale_tail_overrides_to_idle() -> None:
    """A task_started abandoned by a prior process (tail older than process start) reads IDLE."""
    state = derive_codex(
        turn_open=True,
        has_pending_tool_use=True,
        tail_event_at=100.0,
        process_started_at=200.0,
    )
    assert state == ActivityState.IDLE


def test_derive_codex_fresh_open_turn_reports_working() -> None:
    state = derive_codex(
        turn_open=True,
        has_pending_tool_use=False,
        tail_event_at=300.0,
        process_started_at=200.0,
    )
    assert state == ActivityState.THINKING
