"""Tests for :mod:`codex_session_parser` -- mapping raw codex rollout lines to the
web-UI event schema. Focused on the load-bearing invariants: stable, position-
independent event ids (so codex's re-serialised / re-read duplicates dedup), the
user-bubble / turn-abort sourcing, and the self-contained web-search expansion.
"""

from __future__ import annotations

from typing import Any

from imbue.system_interface.codex_session_parser import parse_codex_rollout_line


def _user_line(text: str, timestamp: str = "2026-07-19T10:00:00.123Z") -> dict:
    return {"timestamp": timestamp, "type": "event_msg", "payload": {"type": "user_message", "message": text}}


def test_user_bubble_id_is_stable_across_line_index() -> None:
    """The same user message re-read at a different physical line (e.g. a rollout
    compressed then re-materialised, repointing the marker and forcing a re-read from
    byte 0) must yield the SAME event id so the watcher dedups it -- not a duplicate
    bubble. This is why the id is content-derived, not line-index-derived."""
    line = _user_line("hello codex")
    first = parse_codex_rollout_line(line, 5, {})
    reread = parse_codex_rollout_line(line, 999, {})
    assert first == reread
    assert first[0]["event_id"] == reread[0]["event_id"]
    assert first[0]["type"] == "user_message"
    assert first[0]["content"] == "hello codex"


def test_user_bubble_id_differs_for_distinct_sends() -> None:
    """Distinct sends (different text, or same text at a different time) must NOT
    collide, or a genuine repeat would be swallowed as a duplicate."""
    a = parse_codex_rollout_line(_user_line("yes"), 1, {})[0]["event_id"]
    b = parse_codex_rollout_line(_user_line("no"), 2, {})[0]["event_id"]
    c = parse_codex_rollout_line(_user_line("yes", timestamp="2026-07-19T10:00:05.000Z"), 3, {})[0]["event_id"]
    # different text
    assert a != b
    # same text, different timestamp
    assert a != c


def test_empty_user_message_is_skipped() -> None:
    assert parse_codex_rollout_line(_user_line(""), 0, {}) == []


def test_turn_aborted_emits_marker() -> None:
    """A user interrupt is surfaced as a lightweight turn_aborted marker (used to
    clear a stuck 'Running' dot), not dropped."""
    line = {"timestamp": "2026-07-19T10:00:01Z", "type": "event_msg", "payload": {"type": "turn_aborted"}}
    events = parse_codex_rollout_line(line, 7, {})
    assert len(events) == 1
    assert events[0]["type"] == "turn_aborted"


def test_assistant_message_id_dedups_reserialised_copies() -> None:
    """Codex re-serialises history; each copy keeps the message id, so the event id
    keys on it (stable across the physical line it is re-read at)."""
    line = {
        "timestamp": "2026-07-19T10:00:02Z",
        "type": "response_item",
        "payload": {"type": "message", "role": "assistant", "id": "msg_abc", "content": [{"type": "output_text", "text": "hi"}]},
    }
    first = parse_codex_rollout_line(line, 3, {})
    reread = parse_codex_rollout_line(line, 400, {})
    assert first[0]["event_id"] == reread[0]["event_id"] == "codex-msg_abc"
    assert first[0]["text"] == "hi"


def test_response_item_user_role_is_dropped() -> None:
    """The model-facing role=user item carries injected AGENTS.md / environment
    context; user bubbles come from event_msg, so this is skipped."""
    line = {
        "timestamp": "2026-07-19T10:00:03Z",
        "type": "response_item",
        "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "prompt+injected"}]},
    }
    assert parse_codex_rollout_line(line, 1, {}) == []


def test_web_search_expands_to_matched_call_and_result() -> None:
    """A completed hosted web search is one self-contained web_search_call item; we
    emit BOTH the tool call and its matching result so it renders as a finished
    'Searching the web' bubble, never a stuck unmatched call."""
    line = {
        "timestamp": "2026-07-19T10:00:04Z",
        "type": "response_item",
        "payload": {"type": "web_search_call", "id": "ws_1", "action": {"type": "search", "query": "python asyncio"}},
    }
    events = parse_codex_rollout_line(line, 2, {})
    assert len(events) == 2
    call, result = events
    assert call["type"] == "assistant_message"
    assert call["tool_calls"][0]["tool_name"] == "web_search"
    assert call["tool_calls"][0]["tool_call_id"] == "ws_1"
    assert result["type"] == "tool_result"
    # matches the call -> not stuck
    assert result["tool_call_id"] == "ws_1"
    assert result["output"] == "python asyncio"


def test_function_call_and_output_link_by_call_id() -> None:
    """A function_call registers its name; the later output recovers it by call_id."""
    name_map: dict[str, str] = {}
    call_line = {
        "timestamp": "2026-07-19T10:00:05Z",
        "type": "response_item",
        "payload": {"type": "function_call", "call_id": "c1", "name": "shell", "arguments": '{"cmd":"ls"}'},
    }
    out_line = {
        "timestamp": "2026-07-19T10:00:06Z",
        "type": "response_item",
        "payload": {"type": "function_call_output", "call_id": "c1", "output": "file.txt"},
    }
    call = parse_codex_rollout_line(call_line, 1, name_map)[0]
    out = parse_codex_rollout_line(out_line, 2, name_map)[0]
    assert call["tool_calls"][0]["tool_call_id"] == "c1"
    assert out["tool_call_id"] == "c1"
    # recovered from the cross-line map
    assert out["tool_name"] == "shell"


def test_non_conversation_lines_are_dropped() -> None:
    assert parse_codex_rollout_line({"timestamp": "t", "type": "session_meta", "payload": {"type": "x"}}, 0, {}) == []
    assert parse_codex_rollout_line({"timestamp": "t", "type": "turn_context", "payload": {}}, 0, {}) == []
    non_dict_payload: dict[str, Any] = {"type": "event_msg", "payload": "not-a-dict"}
    assert parse_codex_rollout_line(non_dict_payload, 0, {}) == []
