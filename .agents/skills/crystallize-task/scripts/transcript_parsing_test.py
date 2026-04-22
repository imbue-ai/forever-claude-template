"""Tests for the shared transcript_parsing helpers."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_SCRIPT = Path(__file__).parent / "transcript_parsing.py"
_spec = importlib.util.spec_from_file_location("transcript_parsing", _SCRIPT)
assert _spec is not None and _spec.loader is not None
transcript_parsing = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(transcript_parsing)


def _write_jsonl(path: Path, events: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    return path


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


def test_last_user_message_index_returns_human_boundary() -> None:
    events = [
        {"type": "user", "message": {"content": [{"type": "text", "text": "first"}]}},
        {"type": "assistant", "message": {"content": []}},
        {"type": "user", "message": {"content": [{"type": "text", "text": "second"}]}},
        {"type": "assistant", "message": {"content": []}},
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "x"}]},
        },
    ]
    assert transcript_parsing.last_user_message_index(events) == 2


def test_last_user_message_index_skips_tool_result_carriers() -> None:
    events = [
        {"type": "user", "message": {"content": [{"type": "text", "text": "human"}]}},
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "x"}]},
        },
    ]
    assert transcript_parsing.last_user_message_index(events) == 0


def test_last_user_message_index_returns_none_when_no_user_message() -> None:
    events = [{"type": "assistant", "message": {"content": []}}]
    assert transcript_parsing.last_user_message_index(events) is None


def test_last_user_message_index_returns_none_for_empty_list() -> None:
    assert transcript_parsing.last_user_message_index([]) is None


def test_last_user_message_index_skips_meta_events() -> None:
    events = [
        {"type": "user", "message": {"content": [{"type": "text", "text": "human"}]}},
        {
            "type": "user",
            "isMeta": True,
            "message": {
                "content": [
                    {"type": "text", "text": "Base directory for this skill: /x"}
                ]
            },
        },
        {
            "type": "user",
            "isMeta": True,
            "message": {"content": "Stop hook feedback: ..."},
        },
    ]
    assert transcript_parsing.last_user_message_index(events) == 0


def test_nth_user_message_index_returns_most_recent_for_n_zero() -> None:
    events = [
        {"type": "user", "message": {"content": [{"type": "text", "text": "first"}]}},
        {"type": "assistant", "message": {"content": []}},
        {"type": "user", "message": {"content": [{"type": "text", "text": "second"}]}},
        {"type": "assistant", "message": {"content": []}},
    ]
    assert transcript_parsing.nth_user_message_index(events, 0) == 2


def test_nth_user_message_index_returns_previous_for_n_one() -> None:
    events = [
        {"type": "user", "message": {"content": [{"type": "text", "text": "first"}]}},
        {"type": "assistant", "message": {"content": []}},
        {"type": "user", "message": {"content": [{"type": "text", "text": "second"}]}},
        {"type": "assistant", "message": {"content": []}},
    ]
    assert transcript_parsing.nth_user_message_index(events, 1) == 0


def test_nth_user_message_index_skips_meta_and_tool_results() -> None:
    events = [
        # 0: real human
        {"type": "user", "message": {"content": [{"type": "text", "text": "a"}]}},
        # 1: tool_result carrier (skipped)
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "x"}]},
        },
        # 2: isMeta skill invocation (skipped)
        {
            "type": "user",
            "isMeta": True,
            "message": {
                "content": [{"type": "text", "text": "Base directory for this skill: /x"}]
            },
        },
        # 3: real human
        {"type": "user", "message": {"content": [{"type": "text", "text": "b"}]}},
        # 4: isMeta stop hook (skipped)
        {
            "type": "user",
            "isMeta": True,
            "message": {"content": "Stop hook feedback: ..."},
        },
        # 5: real human
        {"type": "user", "message": {"content": [{"type": "text", "text": "c"}]}},
    ]
    assert transcript_parsing.nth_user_message_index(events, 0) == 5
    assert transcript_parsing.nth_user_message_index(events, 1) == 3
    assert transcript_parsing.nth_user_message_index(events, 2) == 0


def test_nth_user_message_index_returns_none_when_exhausted() -> None:
    events = [
        {"type": "user", "message": {"content": [{"type": "text", "text": "only"}]}},
    ]
    assert transcript_parsing.nth_user_message_index(events, 1) is None
    assert transcript_parsing.nth_user_message_index(events, 99) is None


def test_nth_user_message_index_rejects_negative() -> None:
    import pytest

    with pytest.raises(ValueError):
        transcript_parsing.nth_user_message_index([], -1)


def test_find_marker_index_matches_string_content() -> None:
    events = [
        {"type": "user", "message": {"content": "hello world"}},
        {"type": "user", "message": {"content": "goodbye"}},
    ]
    assert transcript_parsing.find_marker_index(events, "goodbye") == 1


def test_find_marker_index_matches_text_block_content() -> None:
    events = [
        {
            "type": "user",
            "message": {"content": [{"type": "text", "text": "abc DEF ghi"}]},
        },
    ]
    assert transcript_parsing.find_marker_index(events, "DEF") == 0


def test_find_marker_index_respects_start_offset() -> None:
    events = [
        {"type": "user", "message": {"content": "apple"}},
        {"type": "user", "message": {"content": "banana"}},
        {"type": "user", "message": {"content": "apple"}},
    ]
    assert transcript_parsing.find_marker_index(events, "apple") == 0
    assert transcript_parsing.find_marker_index(events, "apple", start=1) == 2


def test_find_marker_index_returns_none_when_missing() -> None:
    events = [{"type": "user", "message": {"content": "hello"}}]
    assert transcript_parsing.find_marker_index(events, "xyz") is None
