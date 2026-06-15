"""Unit tests for the copyable ``claude_p`` helper.

These guard the parts that are easy to get wrong and that the module docstring
promises: flag emission per scenario, the success-vs-error JSON arm handling, and
the session-var scrub. The async wrappers' subprocess execution is not tested
here -- it would need a real ``claude`` binary -- but they are thin shells over
these covered helpers.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent / "claude_p.py"
_spec = importlib.util.spec_from_file_location("claude_p", _SCRIPT)
assert _spec is not None and _spec.loader is not None
claude_p = importlib.util.module_from_spec(_spec)
# Register before exec so the module's frozen (``from __future__ import
# annotations``) dataclasses can resolve their own module via sys.modules.
sys.modules[_spec.name] = claude_p
_spec.loader.exec_module(claude_p)


def test_build_argv_completion_disables_tools_and_sets_system() -> None:
    argv = claude_p._build_argv(
        "classify this",
        model="claude-haiku-4-5",
        system="You are a classifier.",
        append_system=None,
        tools="",
        permission_mode=None,
    )
    assert argv[:3] == ["claude", "-p", "classify this"]
    assert (
        "--output-format" in argv and argv[argv.index("--output-format") + 1] == "json"
    )
    assert argv[argv.index("--system-prompt") + 1] == "You are a classifier."
    # tools="" is the meaningful "disable every tool" value and must be emitted.
    assert argv[argv.index("--tools") + 1] == ""
    assert "--append-system-prompt" not in argv
    assert "--permission-mode" not in argv


def test_build_argv_task_keeps_tools_and_sets_permission_mode() -> None:
    argv = claude_p._build_argv(
        "do work",
        model="claude-haiku-4-5",
        system=None,
        append_system="Only touch runtime/.",
        tools=None,
        permission_mode="bypassPermissions",
    )
    # tools=None leaves the flag off entirely, inheriting the default tool set.
    assert "--tools" not in argv
    assert argv[argv.index("--append-system-prompt") + 1] == "Only touch runtime/."
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert "--system-prompt" not in argv


def _success_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "subtype": "success",
        "is_error": False,
        "result": "the answer",
        "total_cost_usd": 0.0123,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 5,
            "cache_creation_input_tokens": 7,
        },
    }
    payload.update(overrides)
    return payload


def test_parse_result_rejects_non_object_payloads() -> None:
    # claude -p is asked for --output-format json, but a list / number / null is
    # still possible external output; validation must surface it as ClaudeCLIError
    # rather than letting a non-object reach the field reads.
    for payload in ([1, 2, 3], "a string", 42, None):
        with pytest.raises(claude_p.ClaudeCLIError, match="expected result shape"):
            claude_p._parse_result(payload)


def test_parse_result_rejects_wrong_typed_fields() -> None:
    # A present-but-wrong-typed field is malformed output: a non-string result or
    # a non-numeric cost fails validation rather than being silently accepted.
    with pytest.raises(claude_p.ClaudeCLIError, match="expected result shape"):
        claude_p._parse_result(_success_payload(result=123))
    with pytest.raises(claude_p.ClaudeCLIError, match="expected result shape"):
        claude_p._parse_result(_success_payload(total_cost_usd="free"))


def test_parse_result_success_extracts_text_cost_and_usage() -> None:
    result = claude_p._parse_result(_success_payload())
    assert result.text == "the answer"
    assert result.cost_usd == 0.0123
    assert result.usage.input_tokens == 100
    assert result.usage.output_tokens == 20
    assert result.usage.cache_read_tokens == 5
    assert result.usage.cache_write_tokens == 7
    assert result.raw["subtype"] == "success"


def test_parse_result_raises_on_error_arm() -> None:
    payload = {
        "subtype": "error_max_turns",
        "is_error": True,
        "errors": ["hit the turn limit"],
        "total_cost_usd": 0.5,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    with pytest.raises(claude_p.ClaudeCLIError, match="error_max_turns"):
        claude_p._parse_result(payload)


def test_parse_result_error_arm_tolerates_non_string_errors() -> None:
    # claude -p output is external JSON: a non-string element in 'errors' must
    # still raise ClaudeCLIError (with the detail stringified), not a TypeError
    # from str.join inside the error path.
    payload = {
        "subtype": "error_during_execution",
        "is_error": True,
        "errors": [{"code": 42}, "and a string"],
        "total_cost_usd": 0.5,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    with pytest.raises(claude_p.ClaudeCLIError, match="and a string"):
        claude_p._parse_result(payload)


def test_parse_result_raises_when_subtype_not_success() -> None:
    # is_error may be absent/false but a non-success subtype must still raise,
    # rather than be treated as an empty-text success.
    with pytest.raises(claude_p.ClaudeCLIError):
        claude_p._parse_result(_success_payload(subtype="error_during_execution"))


def test_parse_result_raises_on_missing_result_text() -> None:
    payload = _success_payload()
    del payload["result"]
    with pytest.raises(claude_p.ClaudeCLIError, match="result"):
        claude_p._parse_result(payload)


def test_parse_result_raises_on_missing_cost() -> None:
    payload = _success_payload()
    del payload["total_cost_usd"]
    with pytest.raises(claude_p.ClaudeCLIError, match="total_cost_usd"):
        claude_p._parse_result(payload)


def test_parse_result_defaults_cache_tokens_to_zero() -> None:
    result = claude_p._parse_result(
        _success_payload(usage={"input_tokens": 3, "output_tokens": 4})
    )
    assert result.usage.cache_read_tokens == 0
    assert result.usage.cache_write_tokens == 0


def test_parse_result_rejects_malformed_usage_tokens() -> None:
    # The usage block is validated too: a non-integer token count is malformed
    # output and raises rather than silently reading as some default.
    with pytest.raises(claude_p.ClaudeCLIError, match="expected result shape"):
        claude_p._parse_result(_success_payload(usage={"input_tokens": "lots"}))


def test_child_env_unsets_session_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(claude_p._MAIN_CLAUDE_SESSION_ID, "main-session")
    monkeypatch.setenv("MNGR_AGENT_NAME", "lead")
    env = claude_p._child_env()
    assert claude_p._MAIN_CLAUDE_SESSION_ID not in env
    # Without the opt-in, the mngr identity vars are left in place.
    assert env.get("MNGR_AGENT_NAME") == "lead"


def test_child_env_strips_mngr_vars_when_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MNGR_AGENT_NAME", "lead")
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", "/tmp/state")
    env = claude_p._child_env(strip_mngr_agent_vars=True)
    assert "MNGR_AGENT_NAME" not in env
    assert "MNGR_AGENT_STATE_DIR" not in env
    # The real environment is untouched (we only scrub the copy).
    assert os.environ.get("MNGR_AGENT_NAME") == "lead"
