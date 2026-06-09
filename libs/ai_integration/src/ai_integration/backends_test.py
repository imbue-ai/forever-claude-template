import pytest
from anthropic.types import ContentBlock, Message, TextBlock, ToolUseBlock
from anthropic.types import Usage as ApiUsage

from ai_integration.backends import (
    build_api_result,
    build_claude_cli_argv,
    parse_api_content,
    parse_cli_result,
)
from ai_integration.data_types import BillingPath
from ai_integration.errors import ClaudeCLIError


def test_build_argv_emits_system_and_disabled_tools() -> None:
    argv = build_claude_cli_argv(
        prompt="hi",
        model="claude-haiku-4-5",
        system="You are terse.",
        append_system=None,
        tools="",
        permission_mode=None,
        extra_args=None,
    )
    assert argv[:3] == ["claude", "-p", "hi"]
    assert "--system-prompt" in argv
    assert argv[argv.index("--system-prompt") + 1] == "You are terse."
    # tools="" must still emit the flag (disable all tools), distinct from None.
    assert "--tools" in argv
    assert argv[argv.index("--tools") + 1] == ""
    assert "--append-system-prompt" not in argv
    # permission_mode=None must leave the flag off entirely.
    assert "--permission-mode" not in argv


def test_build_argv_omits_tools_flag_when_none() -> None:
    # None means "inherit the default agent tool set" -- the flag is left off
    # entirely (this is the run_task / agentic path).
    argv = build_claude_cli_argv(
        prompt="do work",
        model="claude-haiku-4-5",
        system=None,
        append_system="Extra instructions.",
        tools=None,
        permission_mode=None,
        extra_args=["--add-dir", "/repo"],
    )
    assert "--tools" not in argv
    assert "--system-prompt" not in argv
    assert argv[argv.index("--append-system-prompt") + 1] == "Extra instructions."
    assert argv[-2:] == ["--add-dir", "/repo"]


def test_build_argv_emits_permission_mode() -> None:
    # The agentic run_task path passes a permission mode so headless tool use isn't
    # auto-denied; the flag must be emitted with its value.
    argv = build_claude_cli_argv(
        prompt="do work",
        model="claude-haiku-4-5",
        system=None,
        append_system=None,
        tools=None,
        permission_mode="bypassPermissions",
        extra_args=None,
    )
    assert "--permission-mode" in argv
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"


def _text(text: str) -> TextBlock:
    return TextBlock(type="text", text=text, citations=None)


def _api_message(
    *, content: list[ContentBlock], model: str, input_tokens: int, output_tokens: int
) -> Message:
    return Message(
        id="msg_1",
        type="message",
        role="assistant",
        model=model,
        content=content,
        stop_reason="end_turn",
        stop_sequence=None,
        usage=ApiUsage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def test_parse_api_content_concatenates_text() -> None:
    text, tool_calls = parse_api_content([_text("Hello, "), _text("world")])
    assert text == "Hello, world"
    assert tool_calls == ()


def test_parse_api_content_surfaces_tool_use_blocks() -> None:
    # A forced tool call yields empty text and the structured input in tool_calls.
    text, tool_calls = parse_api_content(
        [
            ToolUseBlock(
                type="tool_use",
                id="tu_1",
                name="record_sentiment",
                input={"sentiment": "positive"},
            )
        ]
    )
    assert text == ""
    assert len(tool_calls) == 1
    assert tool_calls[0].name == "record_sentiment"
    assert tool_calls[0].id == "tu_1"
    assert tool_calls[0].input == {"sentiment": "positive"}


def test_build_api_result_uses_served_model() -> None:
    # The served model id (response.model) can differ from the requested alias; the
    # result must report the served id to honor CompletionResult.model ("served by").
    response = _api_message(
        content=[_text("hi")],
        model="claude-haiku-4-5-20251001",
        input_tokens=10,
        output_tokens=5,
    )
    result = build_api_result(response, requested_model="claude-haiku-4-5")
    assert result.model == "claude-haiku-4-5-20251001"
    assert result.text == "hi"
    assert result.usage is not None
    assert result.usage.input_tokens == 10
    # Cost is estimated from the (priced) served model, not left None.
    assert result.cost_usd is not None


def test_build_api_result_falls_back_to_requested_model() -> None:
    # A response that omits/empties model falls back to the requested model so the
    # field is never blank.
    response = _api_message(
        content=[_text("x")], model="", input_tokens=1, output_tokens=1
    )
    result = build_api_result(response, requested_model="claude-haiku-4-5")
    assert result.model == "claude-haiku-4-5"


def _cli_success(**overrides: object) -> dict[str, object]:
    """A well-formed ``claude -p`` success result, with optional field overrides."""
    data: dict[str, object] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "hi",
        "total_cost_usd": 0.01,
        "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 2},
    }
    data.update(overrides)
    return data


def test_parse_cli_result_extracts_text_usage_cost() -> None:
    result = parse_cli_result(_cli_success(), "claude-haiku-4-5")
    assert result.text == "hi"
    assert result.billing_path is BillingPath.CLAUDE_CLI
    assert result.cost_usd == 0.01
    assert result.usage is not None
    assert result.usage.input_tokens == 10
    assert result.usage.cache_read_tokens == 2


def test_parse_cli_result_error_subtype_raises_with_detail() -> None:
    # An error result (no `result` field, is_error true) must fail loudly and surface
    # the worker's errors -- not be parsed as an empty-text success.
    data = {
        "type": "result",
        "subtype": "error_max_turns",
        "is_error": True,
        "errors": ["hit the turn limit"],
        "total_cost_usd": 0.02,
        "usage": {"input_tokens": 9, "output_tokens": 0},
    }
    with pytest.raises(ClaudeCLIError, match="error_max_turns.*hit the turn limit"):
        parse_cli_result(data, "claude-haiku-4-5")


def test_parse_cli_result_missing_cost_raises() -> None:
    # total_cost_usd is required: a missing cost would otherwise slip past the spend
    # ceiling, so it must raise rather than degrade to None.
    data = _cli_success()
    del data["total_cost_usd"]
    with pytest.raises(ClaudeCLIError):
        parse_cli_result(data, "claude-haiku-4-5")


def test_parse_cli_result_missing_usage_raises() -> None:
    data = _cli_success()
    del data["usage"]
    with pytest.raises(ClaudeCLIError):
        parse_cli_result(data, "claude-haiku-4-5")


def test_parse_cli_result_non_dict_raises() -> None:
    with pytest.raises(ClaudeCLIError):
        parse_cli_result(["not", "a", "dict"], "m")
