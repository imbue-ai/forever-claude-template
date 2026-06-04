"""Tests for the session file watcher."""

import json
import time
from pathlib import Path
from typing import Any

from imbue.system_interface.session_parser import parse_session_lines
from imbue.system_interface.session_watcher import AgentSessionWatcher
from imbue.system_interface.session_watcher import read_complete_lines_since_offset


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


def test_poll_does_not_lose_events_on_partial_writes(tmp_path: Path) -> None:
    """A JSONL line written across two flushes must still be read back in full.

    Regression: the poll previously advanced ``byte_offset`` by the full length of
    every read, even when the trailing bytes were a partial line. The next read then
    started mid-record, the parser silently skipped the malformed line, and the
    activity-state cache stayed pinned to the prior turn's tail (e.g. ``tool_result``
    -> indicator stuck on ``Thinking...`` after the agent finished).

    Driving ``read_complete_lines_since_offset`` directly reproduces the exact offset
    bookkeeping the bug lived in, without spinning up the watcher thread.
    """
    session_file = tmp_path / "session.jsonl"

    assistant_event = {
        "type": "assistant",
        "uuid": "uuid-1",
        "timestamp": "2026-01-01T00:00:00Z",
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-6",
            "content": [{"type": "text", "text": "x" * 3000}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    }
    line = json.dumps(assistant_event) + "\n"
    midpoint = len(line) // 2

    # First flush lands only the first half of the record: nothing is consumed and the
    # offset must stay put rather than skipping past the partial bytes.
    session_file.write_bytes(line[:midpoint].encode())
    offset_after_partial, lines_after_partial = read_complete_lines_since_offset(session_file, 0)
    assert offset_after_partial == 0
    assert lines_after_partial == []

    # Second flush completes the record: the whole line is now consumed and the offset
    # advances exactly to the line boundary.
    with open(session_file, "ab") as f:
        f.write(line[midpoint:].encode())
    offset_after_complete, lines_after_complete = read_complete_lines_since_offset(session_file, offset_after_partial)
    assert lines_after_complete == [line.rstrip("\n")]
    assert offset_after_complete == len(line.encode())

    # The recovered line parses back to the assistant_message that the buggy poll dropped.
    events = parse_session_lines(
        lines_after_complete,
        existing_event_ids=None,
        tool_name_by_call_id={},
        session_id="test-session",
    )
    assert any(e["type"] == "assistant_message" for e in events)


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
    watcher._read_initial_offsets()

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
    watcher._read_initial_offsets()

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
            "content": [
                {"type": "tool_result", "tool_use_id": tool_use_id, "content": "done", "is_error": False}
            ],
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
    watcher._read_initial_offsets()

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


def test_get_step_attribution_is_incremental_and_does_not_double_count(tmp_path: Path) -> None:
    """get_step_attribution reads only bytes appended since the last call, so a
    `tk create` already scanned is not counted twice -- otherwise residual
    counting (creates minus started steps) would be corrupted on every refetch.
    """
    create_one = {
        "type": "assistant",
        "uuid": "a1",
        "timestamp": "2026-01-01T00:00:01Z",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tc1", "name": "Bash", "input": {"command": 'tk create --step "First"'}}],
        },
    }
    agent_state_dir, claude_config_dir, session_id = _setup_agent(tmp_path, [create_one])
    session_file = claude_config_dir / "projects" / "hash123" / f"{session_id}.jsonl"

    watcher = AgentSessionWatcher(
        agent_id="test-agent",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda aid, evts: None,
    )

    first = watcher.get_step_attribution()
    assert first.create_titles_by_session[session_id] == ("First",)

    # Append a second create; the first must not be re-counted.
    create_two = {
        "type": "assistant",
        "uuid": "a2",
        "timestamp": "2026-01-01T00:00:02Z",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tc2", "name": "Bash", "input": {"command": 'tk create --step "Second"'}}
            ],
        },
    }
    with session_file.open("a") as handle:
        handle.write(json.dumps(create_two) + "\n")

    second = watcher.get_step_attribution()
    assert second.create_titles_by_session[session_id] == ("First", "Second")
