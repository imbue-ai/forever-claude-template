"""Tests for the shared transcript_parsing helpers."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).parent / "transcript_parsing.py"
_spec = importlib.util.spec_from_file_location("transcript_parsing", _SCRIPT)
assert _spec is not None and _spec.loader is not None
transcript_parsing = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(transcript_parsing)


def test_iter_transcript_skips_blank_and_malformed(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text(
        '{"type": "user"}\n'
        "\n"
        "not-json\n"
        '   \n'
        '{"type": "assistant"}\n'
        '"a-bare-string"\n',
        encoding="utf-8",
    )
    events = transcript_parsing.iter_transcript(path)
    assert events == [{"type": "user"}, {"type": "assistant"}]


def test_is_user_tool_result_carrier_true_for_all_tool_results() -> None:
    event = {
        "type": "user",
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": "a"},
                {"type": "tool_result", "tool_use_id": "b"},
            ]
        },
    }
    assert transcript_parsing.is_user_tool_result_carrier(event) is True


def test_is_user_tool_result_carrier_false_for_mixed_content() -> None:
    event = {
        "type": "user",
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": "a"},
                {"type": "text", "text": "hi"},
            ]
        },
    }
    assert transcript_parsing.is_user_tool_result_carrier(event) is False


def test_is_user_tool_result_carrier_false_for_human_message() -> None:
    event = {
        "type": "user",
        "message": {"content": [{"type": "text", "text": "hello"}]},
    }
    assert transcript_parsing.is_user_tool_result_carrier(event) is False


def test_is_user_tool_result_carrier_false_for_empty_content() -> None:
    event = {"type": "user", "message": {"content": []}}
    assert transcript_parsing.is_user_tool_result_carrier(event) is False


def test_is_user_tool_result_carrier_false_for_missing_message() -> None:
    assert transcript_parsing.is_user_tool_result_carrier({"type": "user"}) is False
