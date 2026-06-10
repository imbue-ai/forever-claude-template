from typing import Any

import pytest

from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.activity_state import RUNNING_LIFECYCLE_STATES
from imbue.system_interface.activity_state import derive_activity_state
from imbue.system_interface.activity_state import has_unmatched_tool_use
from imbue.system_interface.activity_state import is_transcript_tail_stale
from imbue.system_interface.activity_state import last_event_timestamp
from imbue.system_interface.activity_state import last_event_type
from imbue.system_interface.activity_state import parse_iso_timestamp_to_epoch


def _assistant_with_tool_calls(*tool_call_ids: str) -> dict[str, Any]:
    return {
        "type": "assistant_message",
        "tool_calls": [{"tool_call_id": tcid, "tool_name": "Bash"} for tcid in tool_call_ids],
    }


def _tool_result(tool_call_id: str) -> dict[str, Any]:
    return {"type": "tool_result", "tool_call_id": tool_call_id}


@pytest.mark.parametrize(
    "events, expected",
    [
        pytest.param([], False, id="empty_transcript"),
        pytest.param(
            [
                {"type": "user_message", "content": "hi"},
                {"type": "assistant_message", "tool_calls": []},
            ],
            False,
            id="no_tool_calls",
        ),
        pytest.param([_assistant_with_tool_calls("call_a")], True, id="single_unmatched"),
        pytest.param(
            [_assistant_with_tool_calls("call_a"), _tool_result("call_a")],
            False,
            id="all_matched",
        ),
        pytest.param(
            [_assistant_with_tool_calls("call_a", "call_b"), _tool_result("call_a")],
            True,
            id="partially_matched",
        ),
        # A tool_result that arrives before the matching tool_use (theoretical) still matches.
        pytest.param(
            [_tool_result("call_a"), _assistant_with_tool_calls("call_a")],
            False,
            id="out_of_order_match",
        ),
        pytest.param(
            [{"type": "assistant_message", "tool_calls": [{"tool_name": "Bash"}]}],
            False,
            id="skips_blocks_without_id",
        ),
    ],
)
def test_has_unmatched_tool_use(events: list[dict[str, Any]], expected: bool) -> None:
    assert has_unmatched_tool_use(events) is expected


@pytest.mark.parametrize(
    "events, expected",
    [
        pytest.param([], None, id="empty_transcript"),
        pytest.param(
            [
                {"type": "user_message"},
                {"type": "assistant_message", "tool_calls": []},
            ],
            "assistant_message",
            id="returns_final",
        ),
        pytest.param([{"foo": "bar"}], None, id="missing_type_key"),
    ],
)
def test_last_event_type(events: list[dict[str, Any]], expected: str | None) -> None:
    assert last_event_type(events) == expected


@pytest.mark.parametrize(
    "has_pending_tool_use, tail_event_type, expected",
    [
        pytest.param(
            True,
            "assistant_message",
            ActivityState.TOOL_RUNNING,
            id="tool_running_when_unmatched_tool_use",
        ),
        pytest.param(
            False,
            "user_message",
            ActivityState.THINKING,
            id="thinking_when_last_event_is_user_message",
        ),
        pytest.param(
            False,
            "tool_result",
            ActivityState.THINKING,
            id="thinking_when_last_event_is_tool_result",
        ),
        pytest.param(
            False,
            "assistant_message",
            ActivityState.IDLE,
            id="idle_when_last_event_is_assistant_message",
        ),
        pytest.param(
            False,
            None,
            ActivityState.IDLE,
            id="idle_when_no_events",
        ),
    ],
)
def test_derive_activity_state(
    has_pending_tool_use: bool,
    tail_event_type: str | None,
    expected: ActivityState,
) -> None:
    state = derive_activity_state(
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
def test_derive_activity_state_non_running_agent_is_always_idle(lifecycle_state: str) -> None:
    assert lifecycle_state not in RUNNING_LIFECYCLE_STATES
    state = derive_activity_state(
        is_agent_running=False,
        has_pending_tool_use=True,
        tail_event_type="user_message",
    )
    assert state == ActivityState.IDLE


@pytest.mark.parametrize(
    "events, expected",
    [
        pytest.param([], None, id="empty_transcript"),
        pytest.param(
            [{"type": "tool_result", "timestamp": "2026-06-08T19:42:15.191Z"}],
            "2026-06-08T19:42:15.191Z",
            id="returns_final",
        ),
        pytest.param([{"type": "tool_result"}], None, id="missing_timestamp"),
        pytest.param([{"type": "tool_result", "timestamp": ""}], None, id="empty_timestamp"),
    ],
)
def test_last_event_timestamp(events: list[dict[str, Any]], expected: str | None) -> None:
    assert last_event_timestamp(events) == expected


def test_parse_iso_timestamp_to_epoch_roundtrips() -> None:
    # The same instant expressed as Z-suffixed UTC and as an explicit offset must
    # parse to the same absolute epoch.
    assert parse_iso_timestamp_to_epoch("2026-06-08T19:42:15.191Z") == pytest.approx(
        parse_iso_timestamp_to_epoch("2026-06-08T19:42:15.191+00:00")
    )


@pytest.mark.parametrize(
    "value",
    [pytest.param(None, id="none"), pytest.param("", id="empty"), pytest.param("not-a-timestamp", id="garbage")],
)
def test_parse_iso_timestamp_to_epoch_returns_none_on_bad_input(value: str | None) -> None:
    assert parse_iso_timestamp_to_epoch(value) is None


@pytest.mark.parametrize(
    "tail_event_at, process_started_at, expected",
    [
        pytest.param(100.0, 200.0, True, id="tail_before_process_start_is_stale"),
        pytest.param(200.0, 100.0, False, id="tail_after_process_start_is_fresh"),
        pytest.param(100.0, 100.0, False, id="tail_equal_to_process_start_is_fresh"),
        pytest.param(None, 200.0, False, id="missing_tail_is_not_stale"),
        pytest.param(100.0, None, False, id="missing_marker_is_not_stale"),
    ],
)
def test_is_transcript_tail_stale(
    tail_event_at: float | None, process_started_at: float | None, expected: bool
) -> None:
    assert is_transcript_tail_stale(tail_event_at=tail_event_at, process_started_at=process_started_at) is expected


@pytest.mark.parametrize(
    "has_pending_tool_use, tail_event_type",
    [
        pytest.param(True, "assistant_message", id="would_be_tool_running"),
        pytest.param(False, "tool_result", id="would_be_thinking"),
        pytest.param(False, "user_message", id="would_be_thinking_user_message"),
    ],
)
def test_derive_activity_state_stale_tail_overrides_to_idle(has_pending_tool_use: bool, tail_event_type: str) -> None:
    """A transcript tail older than the current process is shown IDLE, not working.

    This is the restarted-mid-turn case: the agent is running and the transcript
    still ends on an unmatched tool_use / tool_result, but that turn was abandoned
    by a prior process, so the indicator must not read "Thinking..." forever.
    """
    state = derive_activity_state(
        is_agent_running=True,
        has_pending_tool_use=has_pending_tool_use,
        tail_event_type=tail_event_type,
        tail_event_at=100.0,
        process_started_at=200.0,
    )
    assert state == ActivityState.IDLE


def test_derive_activity_state_fresh_tail_still_reports_working() -> None:
    """A tail written after the current process started drives the normal state."""
    state = derive_activity_state(
        is_agent_running=True,
        has_pending_tool_use=True,
        tail_event_type="assistant_message",
        tail_event_at=300.0,
        process_started_at=200.0,
    )
    assert state == ActivityState.TOOL_RUNNING
