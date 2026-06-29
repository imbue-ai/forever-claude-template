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


def _make_queued_command_line(
    uuid: str,
    timestamp: str,
    prompt: str,
    command_mode: str = "prompt",
) -> str:
    """A message the user typed while the agent was busy.

    Claude Code records this as an ``attachment`` of type ``queued_command``,
    never as a normal ``user`` line. ``commandMode`` is ``prompt`` for verbatim
    user text and ``task-notification`` for background-task completion notices.
    """
    return json.dumps(
        {
            "type": "attachment",
            "uuid": uuid,
            "timestamp": timestamp,
            "attachment": {"type": "queued_command", "prompt": prompt, "commandMode": command_mode},
        }
    )


def test_parse_queued_command_attachment_emits_user_message() -> None:
    """A message queued while the agent is busy must surface as a user_message.

    Claude Code stores it as a ``queued_command`` attachment rather than a
    ``user`` line (verified against a real Claude 2.1.160 transcript), and the
    agent answers it without ever writing a ``user`` line. If the parser dropped
    it, the message would never appear as a user bubble and the frontend's
    optimistic "Queued" bubble would never reconcile -- staying up even after the
    agent received and answered the message.
    """
    lines = [_make_queued_command_line("uuid-q", "2026-01-01T00:00:00Z", "actually do gmail instead")]
    events = parse_session_lines(lines)
    assert len(events) == 1
    assert events[0]["type"] == "user_message"
    assert events[0]["content"] == "actually do gmail instead"
    assert events[0]["event_id"] == "uuid-q-queued"


def test_queued_command_reconciles_alongside_normal_turns() -> None:
    """A queued message interleaves correctly with the surrounding conversation."""
    lines = [
        _make_user_line("uuid-1", "2026-01-01T00:00:00Z", "fetch my slack unreads"),
        _make_assistant_line("uuid-2", "2026-01-01T00:00:01Z", "Pulling your Slack unreads."),
        _make_queued_command_line("uuid-3", "2026-01-01T00:00:02Z", "actually do gmail instead"),
        _make_assistant_line("uuid-4", "2026-01-01T00:00:03Z", "Switching to Gmail."),
    ]
    events = parse_session_lines(lines)
    assert [e["type"] for e in events] == [
        "user_message",
        "assistant_message",
        "user_message",
        "assistant_message",
    ]
    assert events[2]["content"] == "actually do gmail instead"


def test_queued_task_notification_attachment_not_emitted() -> None:
    """Background-task notices (commandMode=task-notification) are not user turns."""
    lines = [
        _make_queued_command_line(
            "uuid-n",
            "2026-01-01T00:00:00Z",
            "<task-notification>...</task-notification>",
            command_mode="task-notification",
        )
    ]
    events = parse_session_lines(lines)
    assert events == []


def test_non_queued_command_attachment_ignored() -> None:
    """Other attachment types (hook output, diagnostics, etc.) produce no events."""
    lines = [
        json.dumps(
            {
                "type": "attachment",
                "uuid": "uuid-h",
                "timestamp": "2026-01-01T00:00:00Z",
                "attachment": {"type": "hook_success", "content": "some hook output"},
            }
        )
    ]
    events = parse_session_lines(lines)
    assert events == []


def test_blank_queued_command_not_emitted() -> None:
    """A whitespace-only queued prompt is dropped, like a blank user message."""
    lines = [_make_queued_command_line("uuid-b", "2026-01-01T00:00:00Z", "   ")]
    events = parse_session_lines(lines)
    assert events == []


# Real Claude Code slash-command expansions. The tag ORDER differs between
# custom commands (lead with <command-message>) and built-ins (lead with
# <command-name>), and built-ins indent the trailing tags -- both verified
# against real transcripts. Normalization must handle either.
_CUSTOM_COMMAND_EXPANSION = (
    "<command-message>rebase-merge</command-message>\n"
    "<command-name>/rebase-merge</command-name>\n"
    "<command-args>origin/main</command-args>"
)
_BUILTIN_COMMAND_EXPANSION = (
    "<command-name>/compact</command-name>\n"
    "            <command-message>compact</command-message>\n"
    "            <command-args></command-args>"
)


def test_slash_command_expansion_normalized_to_typed_text() -> None:
    """A slash command renders as the '/name args' the user actually typed.

    Claude Code does not store a slash command verbatim; it expands it into an
    XML-ish <command-name>/<command-args> block. The parser rebuilds the typed
    text so (a) the user bubble shows '/rebase-merge origin/main' rather than the
    raw expansion and (b) it matches what the frontend's optimistic bubble stored,
    so reconciliation (whitespace-normalized content match) succeeds.
    """
    lines = [_make_user_line("uuid-1", "2026-01-01T00:00:00Z", _CUSTOM_COMMAND_EXPANSION)]
    events = parse_session_lines(lines)
    assert len(events) == 1
    assert events[0]["type"] == "user_message"
    assert events[0]["content"] == "/rebase-merge origin/main"


def test_slash_command_expansion_with_empty_args_drops_trailing_space() -> None:
    """A no-argument command (built-in tag order, indented, empty args) yields
    just '/compact' -- the rebuilt text carries no dangling whitespace around the
    (absent) args."""
    lines = [_make_user_line("uuid-1", "2026-01-01T00:00:00Z", _BUILTIN_COMMAND_EXPANSION)]
    events = parse_session_lines(lines)
    assert len(events) == 1
    assert events[0]["content"] == "/compact"


def test_queued_slash_command_expansion_normalized() -> None:
    """A slash command queued while the agent is busy is normalized the same way
    on the queued_command path, so it too reconciles against its optimistic
    bubble."""
    lines = [_make_queued_command_line("uuid-q", "2026-01-01T00:00:00Z", _CUSTOM_COMMAND_EXPANSION)]
    events = parse_session_lines(lines)
    assert len(events) == 1
    assert events[0]["content"] == "/rebase-merge origin/main"


def test_non_command_text_with_angle_brackets_untouched() -> None:
    """Ordinary user text that happens to contain angle brackets but no
    <command-name> tag passes through unchanged."""
    text = "does <Foo> compile when T <: Bar?"
    lines = [_make_user_line("uuid-1", "2026-01-01T00:00:00Z", text)]
    events = parse_session_lines(lines)
    assert len(events) == 1
    assert events[0]["content"] == text


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


def test_agent_tool_use_exposes_description_and_subagent_type() -> None:
    lines = [
        _make_assistant_line(
            "uuid-1",
            "2026-01-01T00:00:00Z",
            "spawning",
            tool_calls=[
                {
                    "id": "toolu_agent",
                    "name": "Agent",
                    "input": {"description": "explore foo", "subagent_type": "Explore", "prompt": "do it"},
                }
            ],
        ),
    ]
    events = parse_session_lines(lines)
    tc = events[0]["tool_calls"][0]
    assert tc["description"] == "explore foo"
    assert tc["subagent_type"] == "Explore"


def test_non_agent_tool_use_has_no_description_or_subagent_type() -> None:
    lines = [
        _make_assistant_line(
            "uuid-1",
            "2026-01-01T00:00:00Z",
            "reading",
            tool_calls=[{"id": "toolu_read", "name": "Read", "input": {"file_path": "/x", "description": "nope"}}],
        ),
    ]
    events = parse_session_lines(lines)
    tc = events[0]["tool_calls"][0]
    assert "description" not in tc
    assert "subagent_type" not in tc


def _make_agent_tool_result_line(
    uuid: str,
    timestamp: str,
    tool_use_id: str,
    output: str,
    structured_agent_id: str | None = None,
) -> str:
    raw: dict[str, Any] = json.loads(_make_tool_result_line(uuid, timestamp, tool_use_id, output))
    if structured_agent_id is not None:
        raw["toolUseResult"] = {"status": "completed", "agentId": structured_agent_id}
    return json.dumps(raw)


def test_agent_tool_result_uses_structured_agent_id() -> None:
    tool_name_by_call_id: dict[str, str] = {"toolu_agent": "Agent"}
    lines = [
        _make_agent_tool_result_line(
            "uuid-a",
            "2026-01-01T00:00:00Z",
            "toolu_agent",
            "Exploration complete.",
            structured_agent_id="abc123",
        ),
    ]
    events = parse_session_lines(lines, tool_name_by_call_id=tool_name_by_call_id)
    assert len(events) == 1
    assert events[0]["type"] == "tool_result"
    assert events[0]["subagent_id"] == "abc123"


def test_agent_tool_result_falls_back_to_text_trailer() -> None:
    tool_name_by_call_id: dict[str, str] = {"toolu_agent": "Agent"}
    lines = [
        _make_agent_tool_result_line(
            "uuid-a",
            "2026-01-01T00:00:00Z",
            "toolu_agent",
            "Exploration complete.\nagentId: legacy999",
            structured_agent_id=None,
        ),
    ]
    events = parse_session_lines(lines, tool_name_by_call_id=tool_name_by_call_id)
    assert len(events) == 1
    assert events[0]["subagent_id"] == "legacy999"


def test_agent_tool_result_without_any_agent_id_omits_field() -> None:
    tool_name_by_call_id: dict[str, str] = {"toolu_agent": "Agent"}
    lines = [
        _make_agent_tool_result_line(
            "uuid-a",
            "2026-01-01T00:00:00Z",
            "toolu_agent",
            "Exploration complete with no link info.",
            structured_agent_id=None,
        ),
    ]
    events = parse_session_lines(lines, tool_name_by_call_id=tool_name_by_call_id)
    assert len(events) == 1
    assert "subagent_id" not in events[0]


def test_agent_tool_result_prefers_structured_over_trailer() -> None:
    tool_name_by_call_id: dict[str, str] = {"toolu_agent": "Agent"}
    lines = [
        _make_agent_tool_result_line(
            "uuid-a",
            "2026-01-01T00:00:00Z",
            "toolu_agent",
            "Done.\nagentId: trailerWins",
            structured_agent_id="structuredWins",
        ),
    ]
    events = parse_session_lines(lines, tool_name_by_call_id=tool_name_by_call_id)
    assert events[0]["subagent_id"] == "structuredWins"


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


def test_tool_output_preserves_tk_transition_past_truncation() -> None:
    """A tk transition line (`Updated <id> -> <status>`) that falls past the
    output truncation limit is preserved, so the progress view never loses a
    step transition when a tk command is batched after verbose output."""
    output = ("x" * 5000) + "\nUpdated s1 -> closed\n"
    lines = [_make_tool_result_line("uuid-trunc", "2026-01-01T00:00:02Z", "toolu_1", output)]
    events = parse_session_lines(lines)
    assert events[0]["type"] == "tool_result"
    assert "Updated s1 -> closed" in events[0]["output"]
    # Still truncated overall (not the full verbose output).
    assert len(events[0]["output"]) < len(output)


def test_tool_output_preserves_tk_step_decoration_past_truncation() -> None:
    """The `tk-step <id> title|summary: ...` decoration lines that a step's
    start/close emit are preserved past the output truncation limit, so the
    progress view can read titles and summaries straight from the transcript."""
    output = (
        ("x" * 5000)
        + "\nUpdated cod-step-abcd -> closed\n"
        + "tk-step cod-step-abcd title: Register the new theme\n"
        + "tk-step cod-step-abcd summary: Wired the theme into the toggle.\n"
    )
    lines = [_make_tool_result_line("uuid-dec", "2026-01-01T00:00:02Z", "toolu_1", output)]
    events = parse_session_lines(lines)
    preserved = events[0]["output"]
    assert "Updated cod-step-abcd -> closed" in preserved
    assert "tk-step cod-step-abcd title: Register the new theme" in preserved
    assert "tk-step cod-step-abcd summary: Wired the theme into the toggle." in preserved
    assert len(preserved) < len(output)


def test_tk_lifecycle_input_preview_is_not_truncated() -> None:
    """tk create/start/close inputs survive past the 200-char input-preview
    limit so the historical input fallback can recover titles and summaries.
    Batched `S1=$(tk create ...)` forms and long `tk close <id> "<summary>"`
    calls both qualify; a long non-tk command is still truncated."""
    batched_create = "\n".join(
        f'S{i}=$(tk create --step "Step number {i} with a fairly long descriptive title here")'
        for i in range(1, 6)
    )
    long_close = 'tk close cod-step-abcd "' + ("a very detailed summary of the work " * 6).strip() + '"'
    long_non_tk = "echo " + ("y" * 400)
    lines = [
        _make_assistant_line(
            "uuid-tk",
            "2026-01-01T00:00:00Z",
            "working",
            tool_calls=[
                {"id": "toolu_create", "name": "Bash", "input": {"command": batched_create}},
                {"id": "toolu_close", "name": "Bash", "input": {"command": long_close}},
                {"id": "toolu_echo", "name": "Bash", "input": {"command": long_non_tk}},
            ],
        ),
    ]
    events = parse_session_lines(lines)
    calls = {tc["tool_call_id"]: tc["input_preview"] for tc in events[0]["tool_calls"]}
    # tk lifecycle inputs kept in full (no truncation marker, full length).
    assert len(calls["toolu_create"]) > 203
    assert not calls["toolu_create"].endswith("...")
    assert "Step number 5" in calls["toolu_create"]
    assert len(calls["toolu_close"]) > 203
    assert "detailed summary" in calls["toolu_close"]
    # Non-tk input still truncated at the 200-char limit.
    assert calls["toolu_echo"].endswith("...")
    assert len(calls["toolu_echo"]) <= 203


def test_tk_mentioned_in_quoted_arg_is_still_truncated() -> None:
    """A long non-tk command that merely *mentions* `tk close ...` inside a
    quoted argument is NOT a tk lifecycle call, so its input_preview is still
    truncated. The shared shlex parser distinguishes this from a real tk
    invocation; the previous substring regex wrongly exempted it."""
    mentions_tk = 'echo "remember to tk close cod-step-x once ' + ("the work is fully done " * 12).strip() + '"'
    assert len(mentions_tk) > 200
    lines = [
        _make_assistant_line(
            "uuid-mention",
            "2026-01-01T00:00:00Z",
            "working",
            tool_calls=[{"id": "toolu_mention", "name": "Bash", "input": {"command": mentions_tk}}],
        ),
    ]
    events = parse_session_lines(lines)
    preview = events[0]["tool_calls"][0]["input_preview"]
    assert preview.endswith("...")
    assert len(preview) <= 203
