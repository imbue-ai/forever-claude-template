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
    # The whole backlog is indexed and marked emitted, so the poll loop has
    # nothing to broadcast for it.
    assert [loc.event_id for loc in state.locators] == ["uuid-0-user", "uuid-1-user"]
    assert state.emitted_count == len(state.locators)
    watcher._poll_for_changes()
    assert collected == []

    # An event appended after priming is still emitted exactly once.
    with open(session_file, "ab") as f:
        f.write((json.dumps(_user_event(2)) + "\n").encode("utf-8"))
    watcher._poll_for_changes()
    assert [e["event_id"] for e in collected] == ["uuid-2-user"]


def _make_agent_tool_use_assistant(
    uuid: str,
    timestamp: str,
    tool_use_id: str,
    description: str,
    prompt: str = "do a thing",
    subagent_type: str = "Explore",
    extra_tool_uses: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = [
        {
            "type": "tool_use",
            "id": tool_use_id,
            "name": "Agent",
            "input": {"description": description, "prompt": prompt, "subagent_type": subagent_type},
        }
    ]
    if extra_tool_uses:
        content.extend(extra_tool_uses)
    return {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": timestamp,
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-7",
            "content": content,
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    }


def _write_subagent_session(
    parent_session_file: Path,
    agent_id: str,
    tool_use_id: str,
    first_timestamp: str,
    *,
    agent_type: str = "Explore",
    description: str = "test sub",
) -> Path:
    """Write a subagent jsonl + meta.json mirroring real Claude Code output.

    The jsonl first line has parentUuid=None and no sourceToolAssistantUUID (as real
    sidechain sessions do); the parent linkage lives in the meta.json `toolUseId`, which
    names the parent Agent tool_use directly.
    """
    subagents_dir = parent_session_file.parent / parent_session_file.stem / "subagents"
    subagents_dir.mkdir(parents=True, exist_ok=True)
    sub_id = f"agent-{agent_id}"
    sub_file = subagents_dir / f"{sub_id}.jsonl"
    first_line = {
        "parentUuid": None,
        "isSidechain": True,
        "agentId": agent_id,
        "type": "user",
        "message": {"role": "user", "content": "the prompt"},
        "uuid": f"sub-first-{agent_id}",
        "timestamp": first_timestamp,
        "sessionId": parent_session_file.stem,
    }
    sub_file.write_text(json.dumps(first_line) + "\n")
    meta = {"agentType": agent_type, "description": description, "toolUseId": tool_use_id}
    (subagents_dir / f"{sub_id}.meta.json").write_text(json.dumps(meta))
    return sub_file


def test_running_subagent_gets_rich_card_from_disk_linkage(tmp_path: Path) -> None:
    """A subagent that has started but not yet returned a tool_result should still
    get subagent_metadata attached to its parent Agent tool_use, sourced from the
    subagent meta.json's toolUseId."""
    parent_assistant_uuid = "assistant-uuid-1"
    parent_events: list[dict[str, Any]] = [
        _make_agent_tool_use_assistant(
            uuid=parent_assistant_uuid,
            timestamp="2026-01-01T00:00:01Z",
            tool_use_id="toolu_running",
            description="explore foo",
        ),
    ]

    agent_state_dir, claude_config_dir, session_id = _setup_agent(tmp_path, parent_events)
    parent_session_file = claude_config_dir / "projects" / "hash123" / f"{session_id}.jsonl"
    _write_subagent_session(
        parent_session_file,
        agent_id="abc123running",
        tool_use_id="toolu_running",
        first_timestamp="2026-01-01T00:00:02Z",
        agent_type="Explore",
        description="explore foo",
    )

    watcher = AgentSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: None,
    )

    events = watcher.get_all_events()
    assistant = next(e for e in events if e["type"] == "assistant_message")
    agent_tc = next(tc for tc in assistant["tool_calls"] if tc["tool_name"] == "Agent")
    assert "subagent_metadata" in agent_tc
    assert agent_tc["subagent_metadata"]["agent_type"] == "Explore"
    assert agent_tc["subagent_metadata"]["description"] == "explore foo"


def test_multiple_agent_tool_uses_link_to_their_subagents(tmp_path: Path) -> None:
    """When one assistant message contains multiple Agent tool_uses, each subagent's
    meta.json toolUseId links it to its specific parent tool_use, regardless of order."""
    parent_assistant_uuid = "assistant-uuid-multi"
    extra: list[dict[str, Any]] = [
        {
            "type": "tool_use",
            "id": "toolu_second",
            "name": "Agent",
            "input": {"description": "second sub", "prompt": "p2", "subagent_type": "Explore"},
        }
    ]
    parent_events: list[dict[str, Any]] = [
        _make_agent_tool_use_assistant(
            uuid=parent_assistant_uuid,
            timestamp="2026-01-01T00:00:01Z",
            tool_use_id="toolu_first",
            description="first sub",
            extra_tool_uses=extra,
        ),
    ]

    agent_state_dir, claude_config_dir, session_id = _setup_agent(tmp_path, parent_events)
    parent_session_file = claude_config_dir / "projects" / "hash123" / f"{session_id}.jsonl"
    # Deliberately link the SECOND tool_use first to prove ordering is irrelevant.
    _write_subagent_session(
        parent_session_file,
        agent_id="bbbbbsecond",
        tool_use_id="toolu_second",
        first_timestamp="2026-01-01T00:00:02Z",
        agent_type="Explore",
        description="second sub",
    )
    _write_subagent_session(
        parent_session_file,
        agent_id="aaaaafirst",
        tool_use_id="toolu_first",
        first_timestamp="2026-01-01T00:00:03Z",
        agent_type="Explore",
        description="first sub",
    )

    watcher = AgentSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: None,
    )

    events = watcher.get_all_events()
    assistant = next(e for e in events if e["type"] == "assistant_message")
    agent_tcs = [tc for tc in assistant["tool_calls"] if tc["tool_name"] == "Agent"]
    assert len(agent_tcs) == 2
    assert agent_tcs[0]["subagent_metadata"]["description"] == "first sub"
    assert agent_tcs[1]["subagent_metadata"]["description"] == "second sub"


def test_falls_back_to_tool_result_linkage_when_subagent_file_absent(tmp_path: Path) -> None:
    """If the subagent file is gone (older session, cleanup), the existing
    tool_result-based linkage should still resolve the metadata when the
    metadata cache happens to be populated."""
    parent_assistant_uuid = "assistant-uuid-historical"
    parent_events: list[dict[str, Any]] = [
        _make_agent_tool_use_assistant(
            uuid=parent_assistant_uuid,
            timestamp="2026-01-01T00:00:01Z",
            tool_use_id="toolu_historical",
            description="legacy sub",
        ),
        {
            "type": "user",
            "uuid": "user-uuid-tr",
            "timestamp": "2026-01-01T00:00:05Z",
            "toolUseResult": {"status": "completed", "agentId": "historicalid"},
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_historical",
                        "content": "done",
                        "is_error": False,
                    }
                ],
            },
        },
    ]

    agent_state_dir, claude_config_dir, _ = _setup_agent(tmp_path, parent_events)
    watcher = AgentSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: None,
    )
    # Seed the metadata cache as if the subagent file once existed but is now gone.
    watcher._subagent_metadata["agent-historicalid"] = {
        "agent_type": "Explore",
        "description": "legacy sub",
        "session_id": "agent-historicalid",
    }

    events = watcher.get_all_events()
    assistant = next(e for e in events if e["type"] == "assistant_message")
    agent_tc = next(tc for tc in assistant["tool_calls"] if tc["tool_name"] == "Agent")
    assert "subagent_metadata" in agent_tc
    assert agent_tc["subagent_metadata"]["description"] == "legacy sub"


def _latest_agent_tool_call(
    collected: list[tuple[str, list[dict[str, Any]]]], parent_uuid: str, tool_use_id: str
) -> dict[str, Any] | None:
    """Return the Agent tool_call dict for the given parent/tool_use across all emissions.

    Re-broadcasts mutate the same event object in place, so the latest view of a
    tool_call reflects whether subagent_metadata has been attached yet.
    """
    found: dict[str, Any] | None = None
    for _agent_id, events in collected:
        for event in events:
            if event.get("type") != "assistant_message" or event.get("message_uuid") != parent_uuid:
                continue
            for tc in event.get("tool_calls", []):
                if tc.get("tool_name") == "Agent" and tc.get("tool_call_id") == tool_use_id:
                    found = tc
    return found


def test_late_subagent_discovery_rebroadcasts_enriched_parent(tmp_path: Path) -> None:
    """Reproduces the live-streaming gap: a parent Agent tool_call is broadcast
    before its subagent jsonl exists, so it goes out without subagent_metadata.
    Once the subagent jsonl appears on a later discovery cycle, the parent must
    be re-broadcast carrying the rich-card metadata."""
    parent_assistant_uuid = "assistant-uuid-late"
    tool_use_id = "toolu_late"
    parent_event = _make_agent_tool_use_assistant(
        uuid=parent_assistant_uuid,
        timestamp="2026-01-01T00:00:01Z",
        tool_use_id=tool_use_id,
        description="explore late",
    )

    # Start with an empty main session so the parent line arrives *after* the
    # watcher has set its read offset -- exactly the streaming sequence.
    agent_state_dir, claude_config_dir, session_id = _setup_agent(tmp_path, [])
    parent_session_file = claude_config_dir / "projects" / "hash123" / f"{session_id}.jsonl"

    collected: list[tuple[str, list[dict[str, Any]]]] = []
    watcher = AgentSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: collected.append((aid, evts)),
    )

    watcher._discover_sessions()
    watcher._prime_caches()

    # The main agent writes the assistant message containing the Agent tool_call.
    with open(parent_session_file, "a") as f:
        f.write(json.dumps(parent_event) + "\n")

    watcher._poll_for_changes()
    broadcast_tc = _latest_agent_tool_call(collected, parent_assistant_uuid, tool_use_id)
    assert broadcast_tc is not None, "parent assistant message should have been broadcast"
    assert "subagent_metadata" not in broadcast_tc, "no metadata before the subagent exists"

    emissions_before = len(collected)

    # The subagent process now spawns and writes its first jsonl line.
    _write_subagent_session(
        parent_session_file,
        agent_id="latesubid",
        tool_use_id=tool_use_id,
        first_timestamp="2026-01-01T00:00:02Z",
        agent_type="Explore",
        description="explore late",
    )

    watcher._discover_sessions()
    watcher._rebroadcast_relinked_parents()

    assert len(collected) == emissions_before + 1, "parent should be re-broadcast once linkage lands"
    relinked_tc = _latest_agent_tool_call(collected, parent_assistant_uuid, tool_use_id)
    assert relinked_tc is not None
    assert relinked_tc["subagent_metadata"]["agent_type"] == "Explore"
    assert relinked_tc["subagent_metadata"]["description"] == "explore late"

    # Idempotent: a fully-linked parent is not re-broadcast again.
    emissions_after_relink = len(collected)
    watcher._rebroadcast_relinked_parents()
    assert len(collected) == emissions_after_relink


def test_inorder_subagent_discovery_does_not_rebroadcast(tmp_path: Path) -> None:
    """When the subagent jsonl already exists by the time the parent is polled,
    the parent is broadcast with metadata directly and there is nothing to
    re-broadcast."""
    parent_assistant_uuid = "assistant-uuid-inorder"
    tool_use_id = "toolu_inorder"
    parent_event = _make_agent_tool_use_assistant(
        uuid=parent_assistant_uuid,
        timestamp="2026-01-01T00:00:01Z",
        tool_use_id=tool_use_id,
        description="explore inorder",
    )

    agent_state_dir, claude_config_dir, session_id = _setup_agent(tmp_path, [])
    parent_session_file = claude_config_dir / "projects" / "hash123" / f"{session_id}.jsonl"

    collected: list[tuple[str, list[dict[str, Any]]]] = []
    watcher = AgentSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: collected.append((aid, evts)),
    )

    watcher._discover_sessions()
    watcher._prime_caches()

    # Subagent linkage is known before the parent line is read.
    _write_subagent_session(
        parent_session_file,
        agent_id="inordersubid",
        tool_use_id=tool_use_id,
        first_timestamp="2026-01-01T00:00:02Z",
        agent_type="Explore",
        description="explore inorder",
    )
    watcher._discover_sessions()

    with open(parent_session_file, "a") as f:
        f.write(json.dumps(parent_event) + "\n")
    watcher._poll_for_changes()

    broadcast_tc = _latest_agent_tool_call(collected, parent_assistant_uuid, tool_use_id)
    assert broadcast_tc is not None
    assert "subagent_metadata" in broadcast_tc, "metadata present on first broadcast"

    emissions_before = len(collected)
    watcher._rebroadcast_relinked_parents()
    assert len(collected) == emissions_before, "nothing left to re-broadcast"


def test_tool_result_in_later_poll_relinks_cached_parent(tmp_path: Path) -> None:
    """On Claude Code versions whose meta.json omits toolUseId, a parent Agent tool_call
    broadcast before its subagent finishes must still upgrade to the rich card the moment
    the subagent's tool_result lands in a LATER poll cycle -- not only on a page refresh.

    This exercises the persistent tool_result linkage: the parent (cycle A) and its
    tool_result (cycle B) never share a poll batch, so the rebroadcast pass must resolve
    the cached parent against the accumulated tool_call_id -> subagent_id map."""
    parent_assistant_uuid = "assistant-uuid-tr"
    tool_use_id = "toolu_tr"
    parent_event = _make_agent_tool_use_assistant(
        uuid=parent_assistant_uuid,
        timestamp="2026-01-01T00:00:01Z",
        tool_use_id=tool_use_id,
        description="explore tr",
    )
    tool_result_line: dict[str, Any] = {
        "type": "user",
        "uuid": "user-uuid-tr",
        "timestamp": "2026-01-01T00:00:09Z",
        "toolUseResult": {"status": "completed", "agentId": "trsubid"},
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": "done", "is_error": False}],
        },
    }

    agent_state_dir, claude_config_dir, session_id = _setup_agent(tmp_path, [])
    parent_session_file = claude_config_dir / "projects" / "hash123" / f"{session_id}.jsonl"

    collected: list[tuple[str, list[dict[str, Any]]]] = []
    watcher = AgentSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: collected.append((aid, evts)),
    )

    watcher._discover_sessions()
    watcher._prime_caches()

    # The subagent's meta.json was discovered (so its agent_type/description are known) but
    # carries no toolUseId on this version (older Claude Code), so only the tool_result
    # agentId can link it.
    watcher._subagent_metadata["agent-trsubid"] = {
        "agent_type": "Explore",
        "description": "explore tr",
        "session_id": "agent-trsubid",
    }

    # Cycle A: the parent assistant message arrives and is broadcast without metadata.
    with open(parent_session_file, "a") as f:
        f.write(json.dumps(parent_event) + "\n")
    watcher._poll_for_changes()
    watcher._rebroadcast_relinked_parents()
    broadcast_tc = _latest_agent_tool_call(collected, parent_assistant_uuid, tool_use_id)
    assert broadcast_tc is not None
    assert "subagent_metadata" not in broadcast_tc, "no metadata while the subagent is still running"

    emissions_before = len(collected)

    # Cycle B (later): the subagent finishes and its tool_result lands in a separate batch.
    with open(parent_session_file, "a") as f:
        f.write(json.dumps(tool_result_line) + "\n")
    watcher._poll_for_changes()
    watcher._rebroadcast_relinked_parents()

    assert len(collected) > emissions_before, "parent should be re-broadcast once the tool_result lands"
    relinked_tc = _latest_agent_tool_call(collected, parent_assistant_uuid, tool_use_id)
    assert relinked_tc is not None
    assert relinked_tc["subagent_metadata"]["description"] == "explore tr"


def test_parent_already_on_disk_at_start_upgrades_card_when_subagent_links(tmp_path: Path) -> None:
    """Conversation opened mid-run: the parent Agent tool_call is already on disk when the
    watcher starts, so priming marks it emitted and the poll loop never re-surfaces it. The
    card must still upgrade live once the subagent links -- the prime-time seed keeps the
    parent eligible for re-broadcast -- rather than staying on "Running..." until a refresh.

    Reproduces the most common real-world trigger: a user clicks into a conversation to watch
    a subagent that was already spawned before they opened it.
    """
    parent_assistant_uuid = "assistant-uuid-midrun"
    tool_use_id = "toolu_midrun"
    parent_event = _make_agent_tool_use_assistant(
        uuid=parent_assistant_uuid,
        timestamp="2026-01-01T00:00:01Z",
        tool_use_id=tool_use_id,
        description="explore midrun",
    )

    # The parent is already on disk before the watcher starts; the subagent does not exist yet.
    agent_state_dir, claude_config_dir, session_id = _setup_agent(tmp_path, [parent_event])
    parent_session_file = claude_config_dir / "projects" / "hash123" / f"{session_id}.jsonl"

    collected: list[tuple[str, list[dict[str, Any]]]] = []
    watcher = AgentSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: collected.append((aid, evts)),
    )

    watcher._discover_sessions()
    watcher._prime_caches()

    # Priming does not broadcast the backlog, and a poll re-surfaces nothing (it was marked
    # emitted), so without the prime-time seed there would be nothing left to upgrade.
    watcher._poll_for_changes()
    assert _latest_agent_tool_call(collected, parent_assistant_uuid, tool_use_id) is None

    # The subagent appears while still running (meta.json present, no tool_result yet).
    _write_subagent_session(
        parent_session_file,
        agent_id="midrunsubid",
        tool_use_id=tool_use_id,
        first_timestamp="2026-01-01T00:00:02Z",
        agent_type="general-purpose",
        description="explore midrun",
    )
    watcher._discover_sessions()
    watcher._rebroadcast_relinked_parents()

    upgraded = _latest_agent_tool_call(collected, parent_assistant_uuid, tool_use_id)
    assert upgraded is not None
    assert upgraded["subagent_metadata"]["session_id"] == "agent-midrunsubid"


def test_tool_result_before_meta_discovery_does_not_strand_card(tmp_path: Path) -> None:
    """The subagent's tool_result is polled before its meta.json is discovered. The parent
    must not be dropped from the re-broadcast cache on bare linkage: it has to stay cached
    until the metadata is actually attached, then upgrade live. Evicting on bare linkage
    (a tool_call_id appearing in a linkage map) stranded the card on "Running..." until a
    page refresh, because the metadata it needed had not been discovered yet.
    """
    parent_assistant_uuid = "assistant-uuid-race"
    tool_use_id = "toolu_race"
    parent_event = _make_agent_tool_use_assistant(
        uuid=parent_assistant_uuid,
        timestamp="2026-01-01T00:00:01Z",
        tool_use_id=tool_use_id,
        description="explore race",
    )
    tool_result_line: dict[str, Any] = {
        "type": "user",
        "uuid": "user-uuid-race",
        "timestamp": "2026-01-01T00:00:05Z",
        "toolUseResult": {"status": "completed", "agentId": "racesubid"},
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": "done", "is_error": False}],
        },
    }

    agent_state_dir, claude_config_dir, session_id = _setup_agent(tmp_path, [])
    parent_session_file = claude_config_dir / "projects" / "hash123" / f"{session_id}.jsonl"

    collected: list[tuple[str, list[dict[str, Any]]]] = []
    watcher = AgentSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: collected.append((aid, evts)),
    )

    watcher._discover_sessions()
    watcher._prime_caches()

    # Cycle A: the parent is broadcast before any linkage exists, and cached.
    with open(parent_session_file, "a") as f:
        f.write(json.dumps(parent_event) + "\n")
    watcher._poll_for_changes()
    watcher._rebroadcast_relinked_parents()
    cycle_a_tc = _latest_agent_tool_call(collected, parent_assistant_uuid, tool_use_id)
    assert cycle_a_tc is not None
    assert "subagent_metadata" not in cycle_a_tc

    # Cycle B: the subagent finishes -- its tool_result lands -- but its meta.json has not
    # been discovered yet. The parent must remain cached, NOT be evicted on bare linkage.
    with open(parent_session_file, "a") as f:
        f.write(json.dumps(tool_result_line) + "\n")
    watcher._poll_for_changes()
    watcher._rebroadcast_relinked_parents()
    assert parent_assistant_uuid in watcher._unlinked_agent_parent_events
    cycle_b_tc = _latest_agent_tool_call(collected, parent_assistant_uuid, tool_use_id)
    assert cycle_b_tc is not None
    assert "subagent_metadata" not in cycle_b_tc

    # Cycle C: the subagent's files are finally discovered; the card upgrades live.
    _write_subagent_session(
        parent_session_file,
        agent_id="racesubid",
        tool_use_id=tool_use_id,
        first_timestamp="2026-01-01T00:00:02Z",
        agent_type="general-purpose",
        description="explore race",
    )
    watcher._discover_sessions()
    watcher._rebroadcast_relinked_parents()
    upgraded = _latest_agent_tool_call(collected, parent_assistant_uuid, tool_use_id)
    assert upgraded is not None
    assert upgraded["subagent_metadata"]["session_id"] == "agent-racesubid"


def test_subagent_discovered_after_history_file_disappears(tmp_path: Path) -> None:
    """A rotated/replaced agent can lose its claude_session_id_history while its main session
    stays watched (already in _session_states). Subagent discovery must still run for known
    sessions, so a subagent that appears AFTER the history file is gone is linked -- not
    stranded on the pending state. (Subagent discovery used to sit behind the history reader's
    early return, so a missing history file silently disabled all further linkage.)"""
    parent_assistant_uuid = "assistant-uuid-rot"
    tool_use_id = "toolu_rot"
    parent_event = _make_agent_tool_use_assistant(
        uuid=parent_assistant_uuid,
        timestamp="2026-01-01T00:00:01Z",
        tool_use_id=tool_use_id,
        description="explore rot",
    )

    agent_state_dir, claude_config_dir, session_id = _setup_agent(tmp_path, [parent_event])
    parent_session_file = claude_config_dir / "projects" / "hash123" / f"{session_id}.jsonl"

    collected: list[tuple[str, list[dict[str, Any]]]] = []
    watcher = AgentSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: collected.append((aid, evts)),
    )

    # The main session is discovered and primed while the history file still exists.
    watcher._discover_sessions()
    watcher._prime_caches()

    # The agent is rotated/replaced: its history file disappears, but the main session file
    # stays on disk and watched.
    (agent_state_dir / "claude_session_id_history").unlink()

    # A subagent appears only now, after the history file is gone.
    _write_subagent_session(
        parent_session_file,
        agent_id="rotsubid",
        tool_use_id=tool_use_id,
        first_timestamp="2026-01-01T00:00:02Z",
        agent_type="general-purpose",
        description="explore rot",
    )

    # Discovery must still pick it up despite the missing history file, and the card links.
    watcher._discover_sessions()
    watcher._rebroadcast_relinked_parents()
    upgraded = _latest_agent_tool_call(collected, parent_assistant_uuid, tool_use_id)
    assert upgraded is not None
    assert upgraded["subagent_metadata"]["session_id"] == "agent-rotsubid"


def test_is_main_session_event_excludes_subagent_sessions(tmp_path: Path) -> None:
    """The predicate that keeps subagent-session events out of the main stream."""
    agent_state_dir, claude_config_dir, session_id = _setup_agent(tmp_path, [])
    watcher = AgentSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: None,
    )
    watcher._discover_sessions()

    assert watcher.is_main_session_event({"session_id": session_id})
    assert not watcher.is_main_session_event({"session_id": "agent-some-subagent"})
    # Events without a session_id (e.g. plugin-injected app events) stay on the main stream.
    assert watcher.is_main_session_event({"type": "agents_updated"})


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


# --- Two-tier evicting cache + bounded tail/backfill ---


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


def test_get_event_offset_reflects_position(tmp_path: Path) -> None:
    """get_event_offset is the global index of an event across resumed files; the
    endpoint returns it so the client can place the loaded window and derive
    whether more history exists above (offset > 0) and below (offset + len < total)."""
    agent_state_dir, claude_config_dir = _build_two_file_agent(tmp_path, file1_lines=40, file2_lines=40)
    watcher = _make_oracle_watcher(agent_state_dir, claude_config_dir, capacity=10_000)
    oracle_ids = _ids(watcher.get_all_events())

    assert watcher.get_event_offset(oracle_ids[0]) == 0
    assert watcher.get_event_offset(oracle_ids[1]) == 1
    # An event in the second file is indexed past the whole first file.
    assert watcher.get_event_offset(oracle_ids[-1]) == len(oracle_ids) - 1
    assert watcher.get_event_offset("does-not-exist") == -1


def test_offset_and_forward_fetch_match_oracle(tmp_path: Path) -> None:
    """get_events_at_offset (jump) and get_forward_events (page newer) equal the
    oracle slices, including across the file-1/file-2 boundary."""
    agent_state_dir, claude_config_dir = _build_two_file_agent(tmp_path, file1_lines=120, file2_lines=80)
    watcher = _make_oracle_watcher(agent_state_dir, claude_config_dir, capacity=10)
    oracle_ids = _ids(watcher.get_all_events())

    # Jump to an arbitrary offset that straddles the file boundary.
    for offset in (0, 5, 115, len(oracle_ids) - 3):
        expected = oracle_ids[offset : offset + 30]
        assert _ids(watcher.get_events_at_offset(offset, 30)) == expected
    # Offset past the end yields nothing.
    assert watcher.get_events_at_offset(len(oracle_ids), 30) == []

    # Forward paging after a cursor, including across the boundary and at the end.
    for cursor_idx in (0, 100, 130, len(oracle_ids) - 1):
        before_id = oracle_ids[cursor_idx]
        expected = oracle_ids[cursor_idx + 1 : cursor_idx + 1 + 30]
        assert _ids(watcher.get_forward_events(before_id, limit=30)) == expected


def test_get_total_event_count_spans_all_files_and_is_window_independent(tmp_path: Path) -> None:
    """The total count covers the whole transcript (across resumed files) and does
    not change with which tail/backfill window has been read -- the client relies
    on it to size the scrollbar for the full conversation, not the loaded slice."""
    agent_state_dir, claude_config_dir = _build_two_file_agent(tmp_path, file1_lines=120, file2_lines=80)
    watcher = _make_oracle_watcher(agent_state_dir, claude_config_dir, capacity=10)
    total = len(watcher.get_all_events())

    assert watcher.get_total_event_count() == total
    # Reading a bounded tail (far smaller than total, and below the body-cache
    # capacity) must not change the reported total.
    watcher.get_tail_events(5)
    assert watcher.get_total_event_count() == total


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
