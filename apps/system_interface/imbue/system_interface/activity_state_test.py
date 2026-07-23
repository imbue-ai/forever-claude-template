from typing import Any

import pytest

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
