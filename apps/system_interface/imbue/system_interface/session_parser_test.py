"""Tests for the session JSONL parser."""

import json
from typing import Any

import pytest

from imbue.system_interface.session_parser import parse_session_lines


def _make_user_line(uuid: str, timestamp: str, content: str) -> str:
    return json.dumps(
        {
            "type": "user",
            "uuid": uuid,
            "timestamp": timestamp,
            "message": {"role": "user", "content": content},
        }
    )


def _make_assistant_line(
    uuid: str,
    timestamp: str,
    text: str,
    tool_calls: list[dict[str, Any]] | None = None,
    model: str = "claude-opus-4-6",
) -> str:
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    if tool_calls:
        for tc in tool_calls:
            content.append(
                {
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["name"],
                    "input": tc.get("input", {}),
                }
            )
    return json.dumps(
        {
            "type": "assistant",
            "uuid": uuid,
            "timestamp": timestamp,
            "message": {
                "role": "assistant",
                "model": model,
                "content": content,
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
        }
    )


def _make_tool_result_line(uuid: str, timestamp: str, tool_use_id: str, output: str) -> str:
    return json.dumps(
        {
            "type": "user",
            "uuid": uuid,
            "timestamp": timestamp,
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": tool_use_id, "content": output, "is_error": False},
                ],
            },
        }
    )


def test_parse_user_message() -> None:
    lines = [_make_user_line("uuid-1", "2026-01-01T00:00:00Z", "Hello")]
    events = parse_session_lines(lines)
    assert len(events) == 1
    assert events[0]["type"] == "user_message"
    assert events[0]["content"] == "Hello"
    assert events[0]["event_id"] == "uuid-1-user"


def test_parse_assistant_message() -> None:
    lines = [_make_assistant_line("uuid-2", "2026-01-01T00:00:01Z", "Hi there!")]
    events = parse_session_lines(lines)
    assert len(events) == 1
    assert events[0]["type"] == "assistant_message"
    assert events[0]["text"] == "Hi there!"
    assert events[0]["model"] == "claude-opus-4-6"
    assert events[0]["tool_calls"] == []


def test_parse_assistant_with_tool_calls() -> None:
    lines = [
        _make_assistant_line(
            "uuid-2",
            "2026-01-01T00:00:01Z",
            "Let me read that.",
            tool_calls=[{"id": "toolu_1", "name": "Read", "input": {"file": "test.txt"}}],
        ),
    ]
    events = parse_session_lines(lines)
    assert len(events) == 1
    assert len(events[0]["tool_calls"]) == 1
    assert events[0]["tool_calls"][0]["tool_name"] == "Read"
    assert events[0]["tool_calls"][0]["tool_call_id"] == "toolu_1"


def test_parse_tool_result() -> None:
    tool_name_by_call_id: dict[str, str] = {"toolu_1": "Read"}
    lines = [_make_tool_result_line("uuid-3", "2026-01-01T00:00:02Z", "toolu_1", "file contents")]
    events = parse_session_lines(lines, tool_name_by_call_id=tool_name_by_call_id)
    assert len(events) == 1
    assert events[0]["type"] == "tool_result"
    assert events[0]["tool_name"] == "Read"
    assert events[0]["output"] == "file contents"


def test_parse_conversation_sequence() -> None:
    lines = [
        _make_user_line("uuid-1", "2026-01-01T00:00:00Z", "Hello"),
        _make_assistant_line("uuid-2", "2026-01-01T00:00:01Z", "Hi!"),
        _make_user_line("uuid-3", "2026-01-01T00:00:02Z", "How are you?"),
        _make_assistant_line("uuid-4", "2026-01-01T00:00:03Z", "Good!"),
    ]
    events = parse_session_lines(lines)
    assert len(events) == 4
    assert events[0]["type"] == "user_message"
    assert events[1]["type"] == "assistant_message"
    assert events[2]["type"] == "user_message"
    assert events[3]["type"] == "assistant_message"


def test_deduplication() -> None:
    lines = [_make_user_line("uuid-1", "2026-01-01T00:00:00Z", "Hello")]
    existing_ids = {"uuid-1-user"}
    events = parse_session_lines(lines, existing_event_ids=existing_ids)
    assert len(events) == 0


def test_skips_non_conversation_events() -> None:
    lines = [
        json.dumps({"type": "progress", "uuid": "uuid-p", "timestamp": "2026-01-01T00:00:00Z"}),
        json.dumps({"type": "file-history-snapshot", "uuid": "uuid-f", "timestamp": "2026-01-01T00:00:00Z"}),
        _make_user_line("uuid-1", "2026-01-01T00:00:01Z", "Hello"),
    ]
    events = parse_session_lines(lines)
    assert len(events) == 1
    assert events[0]["type"] == "user_message"


def test_skips_blank_and_invalid_lines() -> None:
    lines = ["", "  ", "not json", _make_user_line("uuid-1", "2026-01-01T00:00:00Z", "Hello")]
    events = parse_session_lines(lines)
    assert len(events) == 1


def test_tool_result_only_user_message_not_emitted_as_user_message() -> None:
    """A user message containing only tool results should not produce a user_message event."""
    lines = [_make_tool_result_line("uuid-3", "2026-01-01T00:00:02Z", "toolu_1", "result")]
    events = parse_session_lines(lines)
    assert len(events) == 1
    assert events[0]["type"] == "tool_result"


def test_interrupt_sentinel_user_message_not_emitted() -> None:
    """The ``[Request interrupted by user]`` sentinel must not surface as a user_message.

    Claude writes this control text to the user channel when the user interrupts
    a turn. Treating it as a real prompt would leave the activity indicator
    pinned on "Thinking..." after every interrupt, since the indicator's tail-
    event heuristic equates "tail = user_message" with "Claude is about to
    reply." Verify both string content and array content forms.
    """
    string_form = json.dumps(
        {
            "type": "user",
            "uuid": "uuid-1",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": "user", "content": "[Request interrupted by user]"},
        }
    )
    array_form = json.dumps(
        {
            "type": "user",
            "uuid": "uuid-2",
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "[Request interrupted by user]"}],
            },
        }
    )
    events = parse_session_lines([string_form, array_form])
    assert events == []


def test_events_sorted_by_timestamp() -> None:
    lines = [
        _make_assistant_line("uuid-2", "2026-01-01T00:00:02Z", "Second"),
        _make_user_line("uuid-1", "2026-01-01T00:00:01Z", "First"),
    ]
    events = parse_session_lines(lines)
    assert len(events) == 2
    assert events[0]["type"] == "user_message"
    assert events[1]["type"] == "assistant_message"


def test_tool_input_preview_truncation() -> None:
    long_input = {"data": "x" * 300}
    lines = [
        _make_assistant_line(
            "uuid-1",
            "2026-01-01T00:00:00Z",
            "test",
            tool_calls=[{"id": "toolu_1", "name": "Read", "input": long_input}],
        ),
    ]
    events = parse_session_lines(lines)
    preview = events[0]["tool_calls"][0]["input_preview"]
    assert len(preview) <= 203  # 200 + "..."


def test_tool_output_truncation() -> None:
    long_output = "x" * 3000
    tool_name_by_call_id: dict[str, str] = {"toolu_1": "Bash"}
    lines = [_make_tool_result_line("uuid-1", "2026-01-01T00:00:00Z", "toolu_1", long_output)]
    events = parse_session_lines(lines, tool_name_by_call_id=tool_name_by_call_id)
    assert events[0]["output"].endswith("...")
    assert len(events[0]["output"]) <= 2003


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("Here is the file contents.", False, id="plain-assistant-text"),
        pytest.param("", False, id="empty-text"),
        pytest.param(
            "Not logged in \u00b7 Please run /login to authenticate.",
            True,
            id="not-logged-in",
        ),
        pytest.param(
            "I received an error: Invalid API key. Please update your credentials.",
            True,
            id="invalid-api-key",
        ),
        pytest.param(
            "OAuth token has been revoked; re-authentication required.",
            True,
            id="oauth-revoked",
        ),
        pytest.param("Error: OAuth token has expired.", True, id="oauth-expired"),
        pytest.param(
            "OAuth token does not meet scope requirements for this operation.",
            True,
            id="oauth-scope",
        ),
        pytest.param(
            'API returned: {"type": "authentication_error", "message": "..."}',
            True,
            id="authentication-error-type",
        ),
        pytest.param("API Error: 401 Unauthorized", True, id="api-401"),
        pytest.param(
            "Invalid authentication credentials provided.", True, id="invalid-credentials"
        ),
        pytest.param(
            "Your credit balance is too low to make this request.",
            True,
            id="credit-balance-too-low",
        ),
        pytest.param("This organization has been disabled.", True, id="org-disabled"),
    ],
)
def test_assistant_message_auth_error_flag(text: str, expected: bool) -> None:
    lines = [_make_assistant_line("uuid-1", "2026-01-01T00:00:00Z", text)]
    events = parse_session_lines(lines)
    assert events[0]["is_auth_error"] is expected


def test_user_message_with_array_content() -> None:
    line = json.dumps(
        {
            "type": "user",
            "uuid": "uuid-1",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Part one"},
                    {"type": "text", "text": "Part two"},
                ],
            },
        }
    )
    events = parse_session_lines([line])
    assert len(events) == 1
    assert events[0]["content"] == "Part one\nPart two"


def test_resume_continuation_user_message_not_emitted() -> None:
    """Claude Code's isMeta "Continue from where you left off." resume marker
    must not surface as a user_message -- the user never typed it. Claude Code
    injects it (plus a synthetic reply) to close an unfinished turn on resume.
    """
    line = json.dumps(
        {
            "type": "user",
            "uuid": "uuid-r",
            "timestamp": "2026-01-01T00:00:00Z",
            "isMeta": True,
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "Continue from where you left off."}],
            },
        }
    )
    assert parse_session_lines([line]) == []


def test_synthetic_model_assistant_message_not_emitted() -> None:
    """The synthetic "No response requested." reply -- the answer half of the
    resume turn-pair -- is bookkeeping, not a real agent turn, and must not
    surface as an assistant_message event.
    """
    line = json.dumps(
        {
            "type": "assistant",
            "uuid": "uuid-s",
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {
                "role": "assistant",
                "model": "<synthetic>",
                "content": [{"type": "text", "text": "No response requested."}],
                "stop_reason": "stop_sequence",
                "usage": {},
            },
        }
    )
    assert parse_session_lines([line]) == []


def test_resume_marker_filter_is_gated_and_does_not_over_hide() -> None:
    """The resume filters are precise: a human who actually types the
    continuation words (a non-meta message) is still shown, and a real-model
    assistant that happens to say "No response requested." is still shown.
    """
    typed = _make_user_line("uuid-1", "2026-01-01T00:00:00Z", "Continue from where you left off.")
    real_reply = _make_assistant_line("uuid-2", "2026-01-01T00:00:01Z", "No response requested.")
    events = parse_session_lines([typed, real_reply])
    assert [e["type"] for e in events] == ["user_message", "assistant_message"]
    assert events[0]["content"] == "Continue from where you left off."
    assert events[1]["text"] == "No response requested."


def test_synthetic_api_error_message_is_still_shown() -> None:
    """Claude Code stamps the synthetic model on API-error and auth notices
    too (e.g. "API Error: 529 Overloaded", "Please run /login"). Those tell the
    user their turn failed and must stay visible -- only the exact
    "No response requested." resume reply is hidden, not every synthetic message.
    """
    error_text = "API Error: 529 Overloaded. This is a server-side issue, usually temporary."
    line = json.dumps(
        {
            "type": "assistant",
            "uuid": "uuid-e",
            "timestamp": "2026-01-01T00:00:02Z",
            "message": {
                "role": "assistant",
                "model": "<synthetic>",
                "content": [{"type": "text", "text": error_text}],
                "stop_reason": "stop_sequence",
                "usage": {},
            },
        }
    )
    events = parse_session_lines([line])
    assert [e["type"] for e in events] == ["assistant_message"]
    assert events[0]["text"] == error_text
