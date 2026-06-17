"""Unit tests for AgentStreamWatcher.

The watcher tails an agent's ``plugin/claude/stream_buffer`` file (line 1 = uuid
of the last complete assistant message; lines 2.. = in-progress markdown body)
and broadcasts an ``assistant_streaming`` message whenever the (id, body) pair
changes. These tests drive the poll/parse/emit logic directly (no thread) so
they assert exactly when a frame is and is not produced.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from imbue.system_interface.stream_watcher import AgentStreamWatcher
from imbue.system_interface.stream_watcher import STREAMING_MESSAGE_TYPE


def _capture() -> tuple[list[tuple[str, list[dict[str, Any]]]], Any]:
    """Returns (calls, callback) for use as the watcher's on_events arg."""
    calls: list[tuple[str, list[dict[str, Any]]]] = []

    def cb(agent_id: str, events: list[dict[str, Any]]) -> None:
        calls.append((agent_id, events))

    return calls, cb


def _watcher(state_dir: Path) -> tuple[AgentStreamWatcher, list[tuple[str, list[dict[str, Any]]]]]:
    calls, cb = _capture()
    return AgentStreamWatcher(agent_id="agent-1", agent_state_dir=state_dir, on_events=cb), calls


def _write_buffer(state_dir: Path, contents: str) -> None:
    buffer_path = state_dir / "plugin" / "claude" / "stream_buffer"
    buffer_path.parent.mkdir(parents=True, exist_ok=True)
    buffer_path.write_text(contents, encoding="utf-8")


def test_no_buffer_file_emits_nothing(tmp_path: Path) -> None:
    watcher, calls = _watcher(tmp_path)
    watcher._poll_once()
    assert calls == []


def test_emits_in_progress_body_with_id_and_no_session(tmp_path: Path) -> None:
    # Line 1 is the last complete message id; the rest is the streaming body.
    _write_buffer(tmp_path, "uuid-prev\nHello, I am thinking\nabout your request")
    watcher, calls = _watcher(tmp_path)
    watcher._poll_once()

    assert len(calls) == 1
    agent_id, events = calls[0]
    assert agent_id == "agent-1"
    assert len(events) == 1
    frame = events[0]
    assert frame["type"] == STREAMING_MESSAGE_TYPE
    assert frame["last_complete_id"] == "uuid-prev"
    assert frame["text"] == "Hello, I am thinking\nabout your request"
    # No session_id: the frame must ride the main stream and be excluded from
    # per-subagent streams (which filter on a matching session_id).
    assert "session_id" not in frame


def test_unchanged_buffer_does_not_re_emit(tmp_path: Path) -> None:
    _write_buffer(tmp_path, "uuid-prev\npartial response")
    watcher, calls = _watcher(tmp_path)
    watcher._poll_once()
    watcher._poll_once()
    assert len(calls) == 1


def test_growing_body_emits_again(tmp_path: Path) -> None:
    _write_buffer(tmp_path, "uuid-prev\npartial")
    watcher, calls = _watcher(tmp_path)
    watcher._poll_once()
    _write_buffer(tmp_path, "uuid-prev\npartial response, now longer")
    watcher._poll_once()
    assert len(calls) == 2
    assert calls[1][1][0]["text"] == "partial response, now longer"


def test_idle_after_streaming_emits_clearing_frame(tmp_path: Path) -> None:
    # A non-empty body, then the idle state (only the id line remains) -- the
    # change should emit one empty-text frame so the UI clears the bubble, then
    # stay quiet while idle.
    _write_buffer(tmp_path, "uuid-prev\nsome streamed text")
    watcher, calls = _watcher(tmp_path)
    watcher._poll_once()
    _write_buffer(tmp_path, "uuid-new\n")
    watcher._poll_once()
    watcher._poll_once()

    assert len(calls) == 2
    clearing_frame = calls[1][1][0]
    assert clearing_frame["text"] == ""
    assert clearing_frame["last_complete_id"] == "uuid-new"


def test_id_only_buffer_has_empty_body(tmp_path: Path) -> None:
    # A buffer that is only the id line (no trailing newline) is the just-started
    # idle state: empty body, no spurious emit beyond the first.
    _write_buffer(tmp_path, "uuid-prev")
    watcher, calls = _watcher(tmp_path)
    watcher._poll_once()
    # First poll establishes (uuid-prev, "") -- nothing was streaming, so it is
    # the initial state and should not surface a bubble.
    assert calls == []
