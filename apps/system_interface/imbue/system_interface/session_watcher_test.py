"""Tests for the session file watcher."""

import json
import threading
import time
from pathlib import Path
from typing import Any

from imbue.system_interface.session_watcher import AgentSessionWatcher


def _user_event(index: int, content: str | None = None) -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": f"uuid-{index}",
        "timestamp": f"2026-01-01T00:00:{index:02d}Z",
        "message": {"role": "user", "content": content if content is not None else f"Message {index}"},
    }


def _setup_empty_agent(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create an agent whose single session file starts empty.

    Returns (agent_state_dir, claude_config_dir, session_file).
    """
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    claude_config_dir = tmp_path / "claude_config"
    session_dir = claude_config_dir / "projects" / "hash123"
    session_dir.mkdir(parents=True)
    session_file = session_dir / "test-session.jsonl"
    session_file.write_bytes(b"")
    (agent_state_dir / "claude_session_id_history").write_text("test-session\n")
    return agent_state_dir, claude_config_dir, session_file


def _make_watcher(
    agent_state_dir: Path, claude_config_dir: Path, collected: list[dict[str, Any]]
) -> AgentSessionWatcher:
    return AgentSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda _aid, evts: collected.extend(evts),
    )


def _write_session_file(projects_dir: Path, session_id: str, events: list[dict[str, Any]]) -> Path:
    session_dir = projects_dir / "hash123"
    session_dir.mkdir(parents=True, exist_ok=True)
    session_file = session_dir / f"{session_id}.jsonl"
    content = "\n".join(json.dumps(e) for e in events) + "\n"
    session_file.write_text(content)
    return session_file


def _setup_agent(tmp_path: Path, events: list[dict[str, Any]]) -> tuple[Path, Path, str]:
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    claude_config_dir = tmp_path / "claude_config"
    projects_dir = claude_config_dir / "projects"

    session_id = "test-session"
    _write_session_file(projects_dir, session_id, events)
    (agent_state_dir / "claude_session_id_history").write_text(f"{session_id}\n")

    return agent_state_dir, claude_config_dir, session_id


def test_get_all_events_returns_parsed_events(tmp_path: Path) -> None:
    events = [
        {
            "type": "user",
            "uuid": "uuid-1",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": "user", "content": "Hello"},
        },
        {
            "type": "assistant",
            "uuid": "uuid-2",
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-6",
                "content": [{"type": "text", "text": "Hi!"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        },
    ]

    agent_state_dir, claude_config_dir, _ = _setup_agent(tmp_path, events)
    collected: list[tuple[str, list[dict[str, Any]]]] = []

    watcher = AgentSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: collected.append((aid, evts)),
    )

    result = watcher.get_all_events()
    assert len(result) == 2
    assert result[0]["type"] == "user_message"
    assert result[1]["type"] == "assistant_message"


def test_get_all_events_with_tail(tmp_path: Path) -> None:
    events = [
        {
            "type": "user",
            "uuid": f"uuid-{i}",
            "timestamp": f"2026-01-01T00:00:{i:02d}Z",
            "message": {"role": "user", "content": f"Message {i}"},
        }
        for i in range(10)
    ]

    agent_state_dir, claude_config_dir, _ = _setup_agent(tmp_path, events)

    watcher = AgentSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: None,
    )

    result = watcher.get_all_events()
    assert len(result) == 10
    assert result[0]["content"] == "Message 0"
    assert result[9]["content"] == "Message 9"


def test_get_backfill_events(tmp_path: Path) -> None:
    events = [
        {
            "type": "user",
            "uuid": f"uuid-{i}",
            "timestamp": f"2026-01-01T00:00:{i:02d}Z",
            "message": {"role": "user", "content": f"Message {i}"},
        }
        for i in range(10)
    ]

    agent_state_dir, claude_config_dir, _ = _setup_agent(tmp_path, events)

    watcher = AgentSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: None,
    )

    # Get events before uuid-5-user
    result = watcher.get_backfill_events("uuid-5-user", limit=3)
    assert len(result) == 3
    assert result[0]["content"] == "Message 2"
    assert result[2]["content"] == "Message 4"


def test_watcher_detects_new_events(tmp_path: Path) -> None:
    events = [
        {
            "type": "user",
            "uuid": "uuid-1",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": "user", "content": "Hello"},
        },
    ]

    agent_state_dir, claude_config_dir, session_id = _setup_agent(tmp_path, events)
    collected: list[tuple[str, list[dict[str, Any]]]] = []

    watcher = AgentSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: collected.append((aid, evts)),
    )

    # Load initial events (this sets the byte offsets)
    initial = watcher.get_all_events()
    assert len(initial) == 1

    # Start the watcher and give it time to initialize
    watcher.start()
    time.sleep(2.0)  # Allow watcher to fully initialize and set offsets

    try:
        # Append a new event to the session file
        session_file = claude_config_dir / "projects" / "hash123" / f"{session_id}.jsonl"
        new_event = {
            "type": "assistant",
            "uuid": "uuid-2",
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-6",
                "content": [{"type": "text", "text": "Hi!"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        }
        with open(session_file, "a") as f:
            f.write(json.dumps(new_event) + "\n")

        # Wait for the watcher to pick it up
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if collected:
                break
            time.sleep(0.2)

        assert len(collected) >= 1, "Watcher did not detect new events"
        assert collected[0][0] == "test-agent"
        assert collected[0][1][0]["type"] == "assistant_message"
    finally:
        watcher.stop()


def test_watcher_handles_missing_history_file(tmp_path: Path) -> None:
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    claude_config_dir = tmp_path / "claude_config"

    watcher = AgentSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: None,
    )

    # Should not raise
    result = watcher.get_all_events()
    assert len(result) == 0


def test_poll_does_not_lose_record_split_mid_line(tmp_path: Path) -> None:
    """A read landing mid-line must not lose the partial record (issue A)."""
    agent_state_dir, claude_config_dir, session_file = _setup_empty_agent(tmp_path)
    collected: list[dict[str, Any]] = []
    watcher = _make_watcher(agent_state_dir, claude_config_dir, collected)
    watcher._discover_sessions()

    line1 = (json.dumps(_user_event(0)) + "\n").encode("utf-8")
    line2 = (json.dumps(_user_event(1)) + "\n").encode("utf-8")

    # Write line1 fully plus only the first half of line2 (no terminating newline).
    with open(session_file, "ab") as f:
        f.write(line1)
        f.write(line2[: len(line2) // 2])
    watcher._poll_for_changes()

    # Only the complete record is emitted; the partial line is retained, not lost.
    assert [e["event_id"] for e in collected] == ["uuid-0-user"]

    # Flush the rest of line2; the previously-partial record must now appear.
    with open(session_file, "ab") as f:
        f.write(line2[len(line2) // 2 :])
    watcher._poll_for_changes()

    assert [e["event_id"] for e in collected] == ["uuid-0-user", "uuid-1-user"]
    assert collected[1]["content"] == "Message 1"


def test_poll_does_not_corrupt_split_multibyte_utf8(tmp_path: Path) -> None:
    """A read splitting a multi-byte UTF-8 sequence must not corrupt it (issue A)."""
    agent_state_dir, claude_config_dir, session_file = _setup_empty_agent(tmp_path)
    collected: list[dict[str, Any]] = []
    watcher = _make_watcher(agent_state_dir, claude_config_dir, collected)
    watcher._discover_sessions()

    # Content ends with a 4-byte emoji whose UTF-8 sequence we deliberately split.
    content = "café\U0001f389"
    line_bytes = (json.dumps(_user_event(0, content=content), ensure_ascii=False) + "\n").encode("utf-8")
    emoji_bytes = "\U0001f389".encode("utf-8")
    # Land inside the 4-byte sequence.
    split = line_bytes.index(emoji_bytes) + 2

    with open(session_file, "ab") as f:
        f.write(line_bytes[:split])
    watcher._poll_for_changes()
    # The split multi-byte sequence is not yet complete: nothing emitted, no crash.
    assert collected == []

    with open(session_file, "ab") as f:
        f.write(line_bytes[split:])
    watcher._poll_for_changes()

    assert len(collected) == 1
    assert collected[0]["content"] == content


def test_poll_emits_final_record_without_trailing_newline(tmp_path: Path) -> None:
    """A complete final record lacking a trailing newline must still be emitted (issue A)."""
    agent_state_dir, claude_config_dir, session_file = _setup_empty_agent(tmp_path)
    collected: list[dict[str, Any]] = []
    watcher = _make_watcher(agent_state_dir, claude_config_dir, collected)
    watcher._discover_sessions()

    with open(session_file, "ab") as f:
        # Deliberately omit the trailing newline.
        f.write(json.dumps(_user_event(0)).encode("utf-8"))
    watcher._poll_for_changes()

    assert [e["event_id"] for e in collected] == ["uuid-0-user"]


def test_poll_handles_truncation(tmp_path: Path) -> None:
    """A truncated/rotated file must be re-read from the start (issue B)."""
    agent_state_dir, claude_config_dir, session_file = _setup_empty_agent(tmp_path)
    collected: list[dict[str, Any]] = []
    watcher = _make_watcher(agent_state_dir, claude_config_dir, collected)
    watcher._discover_sessions()

    with open(session_file, "ab") as f:
        f.write((json.dumps(_user_event(5)) + "\n").encode("utf-8"))
        f.write((json.dumps(_user_event(6)) + "\n").encode("utf-8"))
    watcher._poll_for_changes()
    assert [e["event_id"] for e in collected] == ["uuid-5-user", "uuid-6-user"]

    # Truncate and rewrite with a shorter, different content. The new file is
    # smaller than the consumed offset; without truncation handling this would
    # be silently ignored.
    session_file.write_bytes((json.dumps(_user_event(1)) + "\n").encode("utf-8"))
    watcher._poll_for_changes()

    assert "uuid-1-user" in [e["event_id"] for e in collected]


def test_poll_re_reads_truncated_file_with_recurring_event_ids(tmp_path: Path) -> None:
    """A truncate-then-rewrite that reuses prior event IDs must re-emit them.

    The agent-wide dedup set retains every event ID it has seen. If the
    truncation reset does not purge the truncated file's IDs, the re-read is
    deduplicated against the stale IDs and silently drops every recurring
    record -- the typical atomic save-rewrite case (issue B follow-up).
    """
    agent_state_dir, claude_config_dir, session_file = _setup_empty_agent(tmp_path)
    collected: list[dict[str, Any]] = []
    watcher = _make_watcher(agent_state_dir, claude_config_dir, collected)
    watcher._discover_sessions()

    original = (json.dumps(_user_event(0)) + "\n").encode("utf-8") + (json.dumps(_user_event(1)) + "\n").encode(
        "utf-8"
    )
    session_file.write_bytes(original)
    watcher._poll_for_changes()
    assert [e["event_id"] for e in collected] == ["uuid-0-user", "uuid-1-user"]

    # Rewrite the file shorter but reusing event 0's ID, then growing again to
    # the same two records. The first record's ID recurs and must reappear.
    session_file.write_bytes((json.dumps(_user_event(0)) + "\n").encode("utf-8"))
    watcher._poll_for_changes()
    session_file.write_bytes(original)
    watcher._poll_for_changes()

    final_state = watcher._session_states["test-session"]
    assert [loc.event_id for loc in final_state.locators] == ["uuid-0-user", "uuid-1-user"]


def test_get_all_events_caches_parsed_events(tmp_path: Path) -> None:
    """Unchanged files are not re-parsed across calls (issue D)."""
    agent_state_dir, claude_config_dir, _ = _setup_agent(tmp_path, [_user_event(i) for i in range(5)])
    watcher = AgentSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: None,
    )

    first = watcher.get_all_events()
    second = watcher.get_all_events()

    # Re-parsing would produce fresh dict objects; cached events share identity.
    assert len(first) == len(second) == 5
    for a, b in zip(first, second, strict=True):
        assert a is b


def test_get_all_events_parses_only_new_tail(tmp_path: Path) -> None:
    """Appending to a file only parses the new tail, reusing cached events (issue D)."""
    agent_state_dir, claude_config_dir, session_id = _setup_agent(tmp_path, [_user_event(i) for i in range(3)])
    watcher = AgentSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: None,
    )

    first = watcher.get_all_events()
    assert len(first) == 3

    session_file = claude_config_dir / "projects" / "hash123" / f"{session_id}.jsonl"
    with open(session_file, "a") as f:
        f.write(json.dumps(_user_event(3)) + "\n")

    second = watcher.get_all_events()
    assert len(second) == 4
    # The original three events are reused (same identity), not re-parsed.
    for a, b in zip(first, second[:3], strict=True):
        assert a is b


def test_concurrent_reads_and_discovery_do_not_raise(tmp_path: Path) -> None:
    """Concurrent get_all_events + session discovery must not raise (issue C).

    Without locking, iterating _session_states while another thread inserts into
    it raises ``RuntimeError: dictionary changed size during iteration``.
    """
    agent_state_dir, claude_config_dir, _ = _setup_agent(tmp_path, [_user_event(0)])
    projects_dir = claude_config_dir / "projects"
    history_file = agent_state_dir / "claude_session_id_history"
    watcher = AgentSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: None,
    )

    errors: list[RuntimeError] = []
    stop = threading.Event()
    discovery_rounds = 60

    def reader() -> None:
        while not stop.is_set():
            try:
                watcher.get_all_events()
            except RuntimeError as e:
                # "dictionary changed size during iteration" is the unlocked failure.
                errors.append(e)

    def discoverer() -> None:
        try:
            for i in range(discovery_rounds):
                session_id = f"extra-session-{i}"
                _write_session_file(projects_dir, session_id, [_user_event(i)])
                with open(history_file, "a") as f:
                    f.write(f"{session_id}\n")
                watcher._discover_sessions()
        finally:
            stop.set()

    reader_thread = threading.Thread(target=reader)
    discoverer_thread = threading.Thread(target=discoverer)
    reader_thread.start()
    discoverer_thread.start()
    discoverer_thread.join(timeout=30.0)
    reader_thread.join(timeout=30.0)

    assert errors == [], f"Concurrent access raised: {errors!r}"


def test_watcher_handles_missing_session_file(tmp_path: Path) -> None:
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    claude_config_dir = tmp_path / "claude_config"
    claude_config_dir.mkdir()

    # Write history with a session ID whose file doesn't exist
    (agent_state_dir / "claude_session_id_history").write_text("nonexistent-session\n")

    watcher = AgentSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: None,
    )

    result = watcher.get_all_events()
    assert len(result) == 0


# --- Two-tier evicting cache + bounded tail/backfill (PR 4a) ---


def _ts(index: int) -> str:
    """A lexicographically sortable, monotonically increasing timestamp."""
    return f"2026-01-01T00:00:00.{index:09d}Z"


def _assistant_line(index: int) -> dict[str, Any]:
    """An assistant message -> exactly one event from one JSONL line."""
    return {
        "type": "assistant",
        "uuid": f"a{index:07d}",
        "timestamp": _ts(index),
        "message": {
            "role": "assistant",
            "model": "claude-test",
            "content": [{"type": "text", "text": f"response {index}"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    }


def _user_multi_line(index: int) -> dict[str, Any]:
    """A user line carrying both text and a tool_result -> TWO events from one line.

    Exercises the multi-event-line path: both the user_message and the
    tool_result share the same source byte range / locator offset.
    """
    return {
        "type": "user",
        "uuid": f"u{index:07d}",
        "timestamp": _ts(index),
        "message": {
            "role": "user",
            "content": [
                {"type": "text", "text": f"message {index}"},
                {"type": "tool_result", "tool_use_id": f"call-{index}", "content": f"output {index}"},
            ],
        },
    }


def _build_two_file_agent(tmp_path: Path, file1_lines: int, file2_lines: int) -> tuple[Path, Path]:
    """Write a resumed conversation across two session files; return (agent_state, claude_config).

    Lines alternate assistant (1 event) and user-multi (2 events) so the
    transcript contains both single- and multi-event lines. Timestamps are
    globally monotonic, with file2 strictly after file1 (a resumed session).
    """
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    claude_config_dir = tmp_path / "claude_config"
    projects_dir = claude_config_dir / "projects"

    counter = 0

    def make_lines(count: int) -> list[dict[str, Any]]:
        nonlocal counter
        lines: list[dict[str, Any]] = []
        for _ in range(count):
            lines.append(_assistant_line(counter) if counter % 2 == 0 else _user_multi_line(counter))
            counter += 1
        return lines

    _write_session_file(projects_dir, "session-1", make_lines(file1_lines))
    _write_session_file(projects_dir, "session-2", make_lines(file2_lines))
    (agent_state_dir / "claude_session_id_history").write_text("session-1\nsession-2\n")
    return agent_state_dir, claude_config_dir


def _make_oracle_watcher(agent_state_dir: Path, claude_config_dir: Path, capacity: int) -> AgentSessionWatcher:
    return AgentSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda _aid, _evts: None,
        body_cache_capacity=capacity,
    )


def _ids(events: list[dict[str, Any]]) -> list[str]:
    return [e["event_id"] for e in events]


def test_tail_and_backfill_match_oracle_across_files(tmp_path: Path) -> None:
    """get_tail_events / get_backfill_events equal the full-transcript oracle slices."""
    agent_state_dir, claude_config_dir = _build_two_file_agent(tmp_path, file1_lines=120, file2_lines=80)
    watcher = _make_oracle_watcher(agent_state_dir, claude_config_dir, capacity=10_000)

    oracle = watcher.get_all_events()
    oracle_ids = _ids(oracle)
    # Multi-event lines produce more events than lines.
    assert len(oracle) > 200

    # Tail matches the end of the oracle.
    assert _ids(watcher.get_tail_events(50)) == oracle_ids[-50:]
    assert _ids(watcher.get_tail_events(1)) == oracle_ids[-1:]

    # Backfill before several cursors -- including one that straddles the
    # file-1 / file-2 boundary -- matches the oracle window.
    for cursor_idx in (5, 50, 130, len(oracle) - 1):
        before_id = oracle_ids[cursor_idx]
        expected = oracle_ids[max(0, cursor_idx - 30) : cursor_idx]
        assert _ids(watcher.get_backfill_events(before_id, limit=30)) == expected

    # Backfill before the very first event yields nothing.
    assert watcher.get_backfill_events(oracle_ids[0], limit=30) == []


def test_has_events_before_reflects_position(tmp_path: Path) -> None:
    agent_state_dir, claude_config_dir = _build_two_file_agent(tmp_path, file1_lines=40, file2_lines=40)
    watcher = _make_oracle_watcher(agent_state_dir, claude_config_dir, capacity=10_000)
    oracle_ids = _ids(watcher.get_all_events())

    assert watcher.has_events_before(oracle_ids[0]) is False
    assert watcher.has_events_before(oracle_ids[1]) is True
    assert watcher.has_events_before(oracle_ids[-1]) is True
    assert watcher.has_events_before("does-not-exist") is False


def test_backfill_of_evicted_history_is_correct(tmp_path: Path) -> None:
    """Backfilling history that has been evicted re-reads it from disk correctly."""
    agent_state_dir, claude_config_dir = _build_two_file_agent(tmp_path, file1_lines=120, file2_lines=80)
    oracle = _make_oracle_watcher(agent_state_dir, claude_config_dir, capacity=10_000)
    oracle_events = oracle.get_all_events()
    oracle_ids = _ids(oracle_events)
    body_by_id = {e["event_id"]: e for e in oracle_events}

    # Tiny cache: anything but the most recent handful is evicted.
    watcher = _make_oracle_watcher(agent_state_dir, claude_config_dir, capacity=16)
    # Prime locators; the tiny cache now holds only the tail.
    watcher.get_tail_events(16)

    # Backfill a window deep in (evicted) history near the start.
    page = watcher.get_backfill_events(oracle_ids[60], limit=20)
    assert _ids(page) == oracle_ids[40:60]
    # Reconstructed bodies match the oracle, not just the ids. Separate ifs
    # (not an if/elif chain) keep the comparison exhaustive per type.
    for event in page:
        oracle_event = body_by_id[event["event_id"]]
        assert event["type"] == oracle_event["type"]
        if event["type"] == "tool_result":
            assert event["output"] == oracle_event["output"]
        if event["type"] == "assistant_message":
            assert event["text"] == oracle_event["text"]


class _CountingWatcher(AgentSessionWatcher):
    """Counts line re-reads so a test can assert backfill disk work is bounded."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.reparse_count = 0

    def _reparse_line_locked(self, state: Any, locator: Any) -> list[dict[str, Any]]:
        self.reparse_count += 1
        return super()._reparse_line_locked(state, locator)


def _count_backfill_reparses(tmp_path: Path, subdir: str, total_lines: int, limit: int) -> int:
    agent_state_dir = tmp_path / subdir / "agent_state"
    agent_state_dir.mkdir(parents=True)
    claude_config_dir = tmp_path / subdir / "claude_config"
    # Assistant-only lines: one event per line, so the page touches exactly
    # `limit` distinct lines -- making the re-read count deterministic.
    _write_session_file(claude_config_dir / "projects", "session-1", [_assistant_line(i) for i in range(total_lines)])
    (agent_state_dir / "claude_session_id_history").write_text("session-1\n")

    watcher = _CountingWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda _aid, _evts: None,
        body_cache_capacity=16,
    )
    # Prime; resolves only the tail.
    ids = _ids(watcher.get_tail_events(16))
    assert ids
    # Page deep in evicted history; this is the operation under measurement.
    watcher.reparse_count = 0
    page = watcher.get_backfill_events(f"a{50:07d}-assistant", limit=limit)
    assert len(page) == limit
    return watcher.reparse_count


def test_backfill_disk_reads_are_bounded_independent_of_transcript_length(tmp_path: Path) -> None:
    """A backfill page re-reads O(limit) lines, regardless of total transcript size.

    A full-file re-read (the pre-PR-4 behavior) would scale the work with the
    transcript length; this asserts the work is identical for a small and a
    large transcript and never exceeds the page size.
    """
    limit = 10
    small = _count_backfill_reparses(tmp_path, "small", total_lines=500, limit=limit)
    large = _count_backfill_reparses(tmp_path, "large", total_lines=4000, limit=limit)

    assert small == large
    assert small <= limit


def test_body_cache_capacity_respected_while_paging_full_history(tmp_path: Path) -> None:
    """Paging backward through the whole transcript keeps resident bodies bounded."""
    agent_state_dir, claude_config_dir = _build_two_file_agent(tmp_path, file1_lines=150, file2_lines=150)
    capacity = 16
    watcher = _make_oracle_watcher(agent_state_dir, claude_config_dir, capacity=capacity)

    oracle_ids = _make_oracle_watcher(agent_state_dir, claude_config_dir, capacity=10_000).get_all_events()
    all_ids = _ids(oracle_ids)

    page_size = 10
    tail = watcher.get_tail_events(page_size)
    seen = _ids(tail)
    assert len(watcher._body_cache) <= capacity

    page = watcher.get_backfill_events(seen[0], limit=page_size)
    # Body cache never exceeds capacity at any point during paging (checked after
    # every call, including the final one that returns an empty page).
    assert len(watcher._body_cache) <= capacity
    while page:
        seen = _ids(page) + seen
        page = watcher.get_backfill_events(seen[0], limit=page_size)
        assert len(watcher._body_cache) <= capacity

    # Despite eviction, paging recovered the entire transcript in order.
    assert seen == all_ids
