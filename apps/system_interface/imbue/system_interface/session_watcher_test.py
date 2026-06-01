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

    original = (json.dumps(_user_event(0)) + "\n").encode("utf-8") + (
        json.dumps(_user_event(1)) + "\n"
    ).encode("utf-8")
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
    assert [e["event_id"] for e in final_state.events] == ["uuid-0-user", "uuid-1-user"]


def test_poll_still_emits_events_parsed_by_a_concurrent_get_all_events(tmp_path: Path) -> None:
    """A concurrent HTTP read must not rob the poll loop of events to emit.

    ``get_all_events`` and the poll loop share the per-file cache offset. If
    emission were driven by what the poll's own parse produced, an
    interleaved ``get_all_events`` that parsed the new tail first would leave
    the poll loop with nothing to emit, and connected SSE clients (which never
    re-fetch) would permanently miss the event. Emission is instead driven by
    ``emitted_count``, so the poll loop still delivers the event exactly once.
    """
    agent_state_dir, claude_config_dir, session_file = _setup_empty_agent(tmp_path)
    collected: list[dict[str, Any]] = []
    watcher = _make_watcher(agent_state_dir, claude_config_dir, collected)
    watcher._discover_sessions()

    with open(session_file, "ab") as f:
        f.write((json.dumps(_user_event(0)) + "\n").encode("utf-8"))

    # Simulate the HTTP path parsing the new tail into the shared cache before
    # the poll loop gets to it.
    watcher.get_all_events()

    watcher._poll_for_changes()
    assert [e["event_id"] for e in collected] == ["uuid-0-user"]

    # A second poll with no new bytes must not re-emit the same event.
    watcher._poll_for_changes()
    assert [e["event_id"] for e in collected] == ["uuid-0-user"]


def test_get_all_events_caches_parsed_events(tmp_path: Path) -> None:
    """Unchanged files are not re-parsed across calls (issue D)."""
    agent_state_dir, claude_config_dir, _ = _setup_agent(
        tmp_path, [_user_event(i) for i in range(5)]
    )
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
    agent_state_dir, claude_config_dir, session_id = _setup_agent(
        tmp_path, [_user_event(i) for i in range(3)]
    )
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


def test_prime_caches_marks_backlog_emitted_atomically(tmp_path: Path) -> None:
    """Priming must mark the existing backlog emitted in the same lock hold that
    fills the cache, so the poll loop never re-broadcasts the backlog while still
    emitting events appended after start.

    The cache fill and the emitted-count mark used to span two separate lock
    acquisitions; a get_all_events landing in the gap could append events that
    then got marked emitted and never reached SSE clients. Priming now marks
    atomically. This asserts the resulting invariant: the whole primed backlog
    is emitted (poll emits nothing for it) and a later append is still emitted.
    """
    agent_state_dir, claude_config_dir, session_file = _setup_empty_agent(tmp_path)
    with open(session_file, "ab") as f:
        f.write((json.dumps(_user_event(0)) + "\n").encode("utf-8"))
        f.write((json.dumps(_user_event(1)) + "\n").encode("utf-8"))

    collected: list[dict[str, Any]] = []
    watcher = _make_watcher(agent_state_dir, claude_config_dir, collected)
    watcher._discover_sessions()
    watcher._prime_caches()

    state = watcher._session_states["test-session"]
    # The whole backlog is cached and marked emitted, so the poll loop has
    # nothing to broadcast for it.
    assert [e["event_id"] for e in state.events] == ["uuid-0-user", "uuid-1-user"]
    assert state.emitted_count == len(state.events)
    watcher._poll_for_changes()
    assert collected == []

    # An event appended after priming is still emitted exactly once.
    with open(session_file, "ab") as f:
        f.write((json.dumps(_user_event(2)) + "\n").encode("utf-8"))
    watcher._poll_for_changes()
    assert [e["event_id"] for e in collected] == ["uuid-2-user"]


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
