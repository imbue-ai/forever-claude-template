"""Tests for the session file watcher."""

import json
import time
from pathlib import Path
from typing import Any

from imbue.system_interface.session_watcher import AgentSessionWatcher


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


def test_watcher_does_not_lose_events_on_partial_writes(tmp_path: Path) -> None:
    """A JSONL line written across two flushes must still be delivered.

    Regression: ``_poll_for_changes`` previously advanced ``byte_offset`` by
    the full length of every read, even when the trailing bytes were a
    partial line. The next poll then started mid-record, the parser silently
    skipped the malformed lines, and the activity-state cache stayed pinned
    to the prior turn's tail (e.g. ``tool_result`` -> indicator stuck on
    ``Thinking...`` after the agent finished).
    """
    agent_state_dir, claude_config_dir, session_id = _setup_agent(tmp_path, [])
    session_file = claude_config_dir / "projects" / "hash123" / f"{session_id}.jsonl"
    collected: list[tuple[str, list[dict[str, Any]]]] = []

    watcher = AgentSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: collected.append((aid, evts)),
    )
    watcher.start()
    time.sleep(2.0)

    try:
        long_text = "x" * 3000
        assistant_event = {
            "type": "assistant",
            "uuid": "uuid-1",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-6",
                "content": [{"type": "text", "text": long_text}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        }
        line = json.dumps(assistant_event) + "\n"
        midpoint = len(line) // 2

        # Simulate a partial flush: write the first half, let the watcher
        # poll, then write the rest.
        with open(session_file, "ab") as f:
            f.write(line[:midpoint].encode())
        time.sleep(1.5)

        with open(session_file, "ab") as f:
            f.write(line[midpoint:].encode())

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if any("assistant_message" == e["type"] for _, evts in collected for e in evts):
                break
            time.sleep(0.2)

        delivered = [e for _, evts in collected for e in evts]
        assert any(e["type"] == "assistant_message" for e in delivered), (
            f"assistant_message dropped after partial write; delivered: {[e['type'] for e in delivered]}"
        )
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
    parent_assistant_uuid: str,
    first_timestamp: str,
    *,
    agent_type: str = "Explore",
    description: str = "test sub",
    write_meta: bool = True,
) -> Path:
    subagents_dir = parent_session_file.parent / parent_session_file.stem / "subagents"
    subagents_dir.mkdir(parents=True, exist_ok=True)
    sub_id = f"agent-{agent_id}"
    sub_file = subagents_dir / f"{sub_id}.jsonl"
    first_line = {
        "parentUuid": parent_assistant_uuid,
        "isSidechain": True,
        "agentId": agent_id,
        "type": "user",
        "message": {"role": "user", "content": "the prompt"},
        "uuid": f"sub-first-{agent_id}",
        "timestamp": first_timestamp,
        "sessionId": parent_session_file.stem,
        "sourceToolAssistantUUID": parent_assistant_uuid,
    }
    sub_file.write_text(json.dumps(first_line) + "\n")
    if write_meta:
        (subagents_dir / f"{sub_id}.meta.json").write_text(
            json.dumps({"agentType": agent_type, "description": description})
        )
    return sub_file


def test_running_subagent_gets_rich_card_from_disk_linkage(tmp_path: Path) -> None:
    """A subagent that has started but not yet returned a tool_result should still
    get subagent_metadata attached to its parent Agent tool_use, sourced from the
    subagent jsonl's first line."""
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
        parent_assistant_uuid=parent_assistant_uuid,
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


def test_multiple_agent_tool_uses_match_subagents_in_spawn_order(tmp_path: Path) -> None:
    """When one assistant message contains multiple Agent tool_uses, subagent
    files written earliest should be paired with tool_uses listed first."""
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
    _write_subagent_session(
        parent_session_file,
        agent_id="aaaaafirst",
        parent_assistant_uuid=parent_assistant_uuid,
        first_timestamp="2026-01-01T00:00:02Z",
        agent_type="Explore",
        description="first sub",
    )
    _write_subagent_session(
        parent_session_file,
        agent_id="bbbbbsecond",
        parent_assistant_uuid=parent_assistant_uuid,
        first_timestamp="2026-01-01T00:00:03Z",
        agent_type="Explore",
        description="second sub",
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
