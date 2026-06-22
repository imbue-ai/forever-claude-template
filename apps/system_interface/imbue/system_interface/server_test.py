"""Tests for the Flask server."""

import json
import queue
from pathlib import Path
from unittest.mock import patch

import pytest
from flask import Flask
from flask.testing import FlaskClient
from mngr_cli_contract.contract import assert_mngr_argv_valid

from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.mngr.errors import AgentStartError
from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.agent_manager import AgentManager
from imbue.system_interface.app_context import state_of
from imbue.system_interface.config import Config
from imbue.system_interface.event_queues import AgentEventQueues
from imbue.system_interface.layout_ops import LayoutMutex
from imbue.system_interface.models import AgentStateItem
from imbue.system_interface.server import _DEFAULT_TAIL_COUNT
from imbue.system_interface.server import _build_destroy_command
from imbue.system_interface.server import _stream_filtered_events
from imbue.system_interface.server import create_application
from imbue.system_interface.testing import close_ws
from imbue.system_interface.testing import open_ws
from imbue.system_interface.testing import serve_app
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster

_WS_RECEIVE_TIMEOUT = 5.0


@pytest.fixture
def config() -> Config:
    return Config()


@pytest.fixture
def app(config: Config) -> Flask:
    return create_application(config)


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


def test_index_returns_html_when_static_exists(client: FlaskClient, tmp_path: Path) -> None:
    """When the static dir has index.html, the server serves it."""
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html><body>test</body></html>")

    with patch("imbue.system_interface.server.STATIC_DIRECTORY", static_dir):
        test_client = create_application().test_client()
        response = test_client.get("/")
        assert response.status_code == 200
        assert "test" in response.text


def test_index_returns_not_built_when_no_static(client: FlaskClient, tmp_path: Path) -> None:
    """When static dir has no index.html, show a helpful message."""
    empty_dir = tmp_path / "static"
    empty_dir.mkdir()

    with patch("imbue.system_interface.server.STATIC_DIRECTORY", empty_dir):
        test_client = create_application().test_client()
        response = test_client.get("/")
        assert response.status_code == 200
        assert "npm run build" in response.text


def test_list_agents_endpoint(client: FlaskClient) -> None:
    """The agents endpoint returns agent data."""
    with patch("imbue.system_interface.server.discover_agents") as mock_discover:
        mock_discover.return_value = [
            AgentInfo(
                id="agent-123",
                name="test-agent",
                state="RUNNING",
                agent_state_dir=Path("/tmp/test"),
                claude_config_dir=Path("/tmp/.claude"),
            )
        ]
        response = client.get("/api/agents")

    assert response.status_code == 200
    data = response.get_json()
    assert len(data["agents"]) == 1
    assert data["agents"][0]["name"] == "test-agent"
    assert data["agents"][0]["state"] == "RUNNING"


def test_get_events_for_unknown_agent(client: FlaskClient) -> None:
    """Getting events for a nonexistent agent returns 404."""
    with patch("imbue.system_interface.server.discover_agents", return_value=[]):
        response = client.get("/api/agents/nonexistent/events")
    assert response.status_code == 404


def test_send_message_for_unknown_agent(client: FlaskClient) -> None:
    """Sending a message to a nonexistent agent returns 404."""
    with patch("imbue.system_interface.server.discover_agents", return_value=[]):
        response = client.post("/api/agents/nonexistent/message", json={"message": "hello"})
    assert response.status_code == 404


def test_get_events_with_session_files(client: FlaskClient, tmp_path: Path) -> None:
    """Getting events for an agent with session files returns parsed events."""
    # Set up agent state dir with session history
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir(parents=True)

    # Create a session file
    claude_config_dir = tmp_path / "claude_config"
    projects_dir = claude_config_dir / "projects" / "hash123"
    projects_dir.mkdir(parents=True)

    session_id = "test-session-id"
    session_file = projects_dir / f"{session_id}.jsonl"
    session_file.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "uuid-1",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"role": "user", "content": "Hello"},
            }
        )
        + "\n"
        + json.dumps(
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
            }
        )
        + "\n"
    )

    # Write session history
    (agent_state_dir / "claude_session_id_history").write_text(f"{session_id}\n")

    agent_info = AgentInfo(
        id="agent-123",
        name="test-agent",
        state="RUNNING",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
    )
    with patch("imbue.system_interface.server._find_agent", return_value=agent_info):
        response = client.get("/api/agents/agent-123/events")

    assert response.status_code == 200
    data = response.get_json()
    assert len(data["events"]) == 2
    assert data["events"][0]["type"] == "user_message"
    assert data["events"][0]["content"] == "Hello"
    assert data["events"][1]["type"] == "assistant_message"
    assert data["events"][1]["text"] == "Hi!"


def test_get_events_caps_initial_load_to_tail(client: FlaskClient, tmp_path: Path) -> None:
    """The no-`before` events response is capped to the most recent N events,
    and older events remain reachable via the `before` backfill branch (issue I)."""
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir(parents=True)
    claude_config_dir = tmp_path / "claude_config"
    projects_dir = claude_config_dir / "projects" / "hash123"
    projects_dir.mkdir(parents=True)

    total_events = _DEFAULT_TAIL_COUNT + 10
    session_id = "test-session-id"
    session_file = projects_dir / f"{session_id}.jsonl"
    session_file.write_text(
        "".join(
            json.dumps(
                {
                    "type": "user",
                    "uuid": f"uuid-{i:03d}",
                    "timestamp": f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z",
                    "message": {"role": "user", "content": f"Message {i}"},
                }
            )
            + "\n"
            for i in range(total_events)
        )
    )
    (agent_state_dir / "claude_session_id_history").write_text(f"{session_id}\n")

    agent_info = AgentInfo(
        id="agent-123",
        name="test-agent",
        state="RUNNING",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
    )

    with patch("imbue.system_interface.server._find_agent", return_value=agent_info):
        response = client.get("/api/agents/agent-123/events")
        assert response.status_code == 200
        body = response.get_json()
        events = body["events"]
        # Only the most recent _DEFAULT_TAIL_COUNT events are returned.
        assert len(events) == _DEFAULT_TAIL_COUNT
        assert events[0]["content"] == f"Message {total_events - _DEFAULT_TAIL_COUNT}"
        assert events[-1]["content"] == f"Message {total_events - 1}"
        # offset + total place the tail window in the full conversation: the first
        # tail event sits at index (total - tail), so offset > 0 tells the client
        # there is older history above to page in.
        assert body["total"] == total_events
        assert body["offset"] == total_events - _DEFAULT_TAIL_COUNT

        # Older events are still reachable by paging backwards from the oldest
        # event in the initial tail.
        oldest_in_tail = events[0]["event_id"]
        backfill = client.get(f"/api/agents/agent-123/events?before={oldest_in_tail}")
        assert backfill.status_code == 200
        backfill_body = backfill.get_json()
        backfill_events = backfill_body["events"]
        assert len(backfill_events) == total_events - _DEFAULT_TAIL_COUNT
        assert backfill_events[0]["content"] == "Message 0"
        assert backfill_events[-1]["content"] == f"Message {total_events - _DEFAULT_TAIL_COUNT - 1}"
        # The page reached the very first event (offset 0 => no more history above).
        assert backfill_body["offset"] == 0
        assert backfill_body["total"] == total_events

        # A jump lands a window at an arbitrary global offset in one request,
        # rather than paging through everything before it.
        jump = client.get("/api/agents/agent-123/events?offset=5&limit=4")
        assert jump.status_code == 200
        jump_body = jump.get_json()
        assert [e["content"] for e in jump_body["events"]] == [f"Message {i}" for i in range(5, 9)]
        assert jump_body["offset"] == 5

        # From that jumped window the client can page *newer* (toward the tail).
        after_id = jump_body["events"][-1]["event_id"]
        forward = client.get(f"/api/agents/agent-123/events?after={after_id}&limit=3")
        assert forward.status_code == 200
        forward_body = forward.get_json()
        assert [e["content"] for e in forward_body["events"]] == [f"Message {i}" for i in range(9, 12)]
        assert forward_body["offset"] == 9

        # A non-positive limit must not defeat the cap (``[-0:]`` would return
        # the whole list); it falls back to the default tail count.
        zero_limit = client.get("/api/agents/agent-123/events?limit=0")
        assert zero_limit.status_code == 200
        assert len(zero_limit.get_json()["events"]) == _DEFAULT_TAIL_COUNT


def test_send_message_success(client: FlaskClient) -> None:
    """Sending a message to a known agent addresses it by id and succeeds."""
    agent_id = "agent-00000000000000000000000000000001"
    agent_info = AgentInfo(
        id=agent_id,
        name="test-agent",
        state="RUNNING",
        agent_state_dir=Path("/tmp/test"),
        claude_config_dir=Path("/tmp/.claude"),
    )
    with (
        patch("imbue.system_interface.server._find_agent", return_value=agent_info),
        patch("imbue.system_interface.agent_manager.send_message", return_value=True) as mock_send,
    ):
        response = client.post(f"/api/agents/{agent_id}/message", json={"message": "hello"})

    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"
    assert mock_send.call_count == 1
    # The endpoint routes through AgentManager.send_message_to_agent, which addresses
    # the agent by id and supplies the live cache's known location as the 3rd arg.
    assert mock_send.call_args.args[0] == agent_id
    assert mock_send.call_args.args[1] == "hello"


def test_interrupt_agent_returns_404_for_unknown_agent(client: FlaskClient) -> None:
    """Interrupting a nonexistent agent returns 404."""
    with patch("imbue.system_interface.server._find_agent", return_value=None):
        response = client.post("/api/agents/nonexistent/interrupt")
    assert response.status_code == 404


def test_interrupt_agent_success(client: FlaskClient) -> None:
    """Interrupting an agent restarts it via mngr and returns 200."""
    agent_info = AgentInfo(
        id="agent-123",
        name="claude-agent",
        state="RUNNING",
        agent_state_dir=Path("/tmp/test"),
        claude_config_dir=Path("/tmp/.claude"),
    )
    fake_result = FinishedProcess(
        returncode=0,
        stdout="Restarted agent: claude-agent",
        stderr="",
        command=("mngr", "start", "claude-agent", "--restart", "--no-resume"),
        is_output_already_logged=False,
    )
    with (
        patch("imbue.system_interface.server._find_agent", return_value=agent_info),
        patch(
            "imbue.system_interface.server.run_local_command_modern_version",
            return_value=fake_result,
        ) as mock_run,
        patch.object(AgentManager, "reset_activity_state") as mock_reset,
    ):
        response = client.post("/api/agents/agent-123/interrupt")

    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"
    assert mock_run.call_args.kwargs["command"] == [
        "mngr",
        "start",
        "claude-agent",
        "--restart",
        "--no-resume",
    ]
    # After a successful restart the endpoint resets the agent's activity
    # state so the indicator clears instead of staying pinned at THINKING.
    mock_reset.assert_called_once_with("agent-123")


def test_interrupt_agent_rejects_is_primary_agent(client: FlaskClient) -> None:
    """POST /api/agents/<id>/interrupt returns 400 for the services agent.

    Restarting the is_primary agent would stop the workspace services. The
    frontend hides such agents; this server-side guard protects direct callers.
    """
    services_agent = AgentInfo(
        id="services-1",
        name="system-services",
        state="RUNNING",
        agent_state_dir=Path("/tmp/test"),
        claude_config_dir=Path("/tmp/.claude"),
        labels={"is_primary": "true", "workspace": "my-ws"},
    )
    with (
        patch("imbue.system_interface.server._find_agent", return_value=services_agent),
        patch("imbue.system_interface.server.run_local_command_modern_version") as mock_run,
    ):
        response = client.post("/api/agents/services-1/interrupt")

    assert response.status_code == 400
    assert "is_primary" in response.get_json()["detail"]
    # The guard runs before the restart subprocess, so mngr is never invoked.
    mock_run.assert_not_called()


def test_interrupt_agent_returns_500_on_failure(client: FlaskClient) -> None:
    """If the mngr restart command exits non-zero, return 500 with its stderr."""
    agent_info = AgentInfo(
        id="agent-123",
        name="claude-agent",
        state="RUNNING",
        agent_state_dir=Path("/tmp/test"),
        claude_config_dir=Path("/tmp/.claude"),
    )
    fake_result = FinishedProcess(
        returncode=1,
        stdout="",
        stderr="mngr start failed",
        command=("mngr", "start", "claude-agent", "--restart", "--no-resume"),
        is_output_already_logged=False,
    )
    with (
        patch("imbue.system_interface.server._find_agent", return_value=agent_info),
        patch(
            "imbue.system_interface.server.run_local_command_modern_version",
            return_value=fake_result,
        ),
    ):
        response = client.post("/api/agents/agent-123/interrupt")

    assert response.status_code == 500
    assert response.get_json()["detail"] == "Failed to interrupt agent 'claude-agent': mngr start failed"


def test_get_layout_returns_404_when_no_layout_saved(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Getting layout returns 404 when no layout file exists."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")
    response = client.get("/api/layout")

    assert response.status_code == 404


def test_save_and_get_layout(client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Saving and retrieving a layout round-trips correctly."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")

    layout_data = {"dockview": {"panels": {}}, "panelParams": {"chat-1": {"panelType": "chat"}}}

    save_response = client.post("/api/layout", json=layout_data)
    assert save_response.status_code == 200
    assert save_response.get_json()["status"] == "ok"

    get_response = client.get("/api/layout")
    assert get_response.status_code == 200
    assert get_response.get_json() == layout_data


def test_save_layout_creates_directory(client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Saving a layout creates the workspace_layout directory if needed."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")

    client.post("/api/layout", json={"test": True})

    assert (tmp_path / "agents" / "agent-123" / "workspace_layout" / "layout.json").exists()


def test_save_layout_rejects_invalid_json(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Saving invalid JSON returns 400."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")
    response = client.post(
        "/api/layout",
        data=b"not valid json",
        content_type="application/json",
    )

    assert response.status_code == 400


def test_index_injects_hostname_meta_tag(tmp_path: Path) -> None:
    """The index page includes a hostname meta tag."""
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html><head></head><body>test</body></html>")

    with patch("imbue.system_interface.server.STATIC_DIRECTORY", static_dir):
        test_client = create_application().test_client()
        response = test_client.get("/")
        assert response.status_code == 200
        assert "system-interface-hostname" in response.text


def test_random_name_endpoint(client: FlaskClient) -> None:
    """The random name endpoint returns a non-empty name."""
    response = client.get("/api/random-name")
    assert response.status_code == 200
    data = response.get_json()
    assert "name" in data
    assert len(data["name"]) > 0


def test_create_chat_agent_without_work_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Creating a chat agent without a primary agent work dir returns 400."""
    monkeypatch.delenv("MNGR_AGENT_WORK_DIR", raising=False)
    monkeypatch.delenv("MNGR_AGENT_ID", raising=False)
    test_client = create_application().test_client()
    response = test_client.post(
        "/api/agents/create-chat",
        json={"name": "test-chat"},
    )
    assert response.status_code == 400


def test_create_worktree_agent_missing_agent(client: FlaskClient) -> None:
    """Creating a worktree agent with an unknown selected agent returns 400."""
    response = client.post(
        "/api/agents/create-worktree",
        json={"name": "test-worktree", "selected_agent_id": "nonexistent"},
    )
    assert response.status_code == 400


@pytest.mark.timeout(15)
def test_websocket_endpoint_sends_initial_snapshot(app: Flask) -> None:
    """The WebSocket endpoint sends agents_updated and applications_updated on connect."""
    with serve_app(app) as served:
        ws = open_ws(served, "/api/ws")
        try:
            msg1 = json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT))
            msg2 = json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT))
        finally:
            close_ws(ws)

    types = {msg1["type"], msg2["type"]}
    assert "agents_updated" in types
    assert "applications_updated" in types


@pytest.mark.timeout(15)
def test_layout_broadcast_open_emits_ws_message(app: Flask) -> None:
    """POST /api/layout/broadcast with op=open emits a layout_op WS message."""
    client = app.test_client()
    with serve_app(app) as served:
        ws = open_ws(served, "/api/ws")
        try:
            # Drain the initial snapshot messages.
            json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT))
            json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT))

            response = client.post(
                "/api/layout/broadcast",
                json={"op": "open", "args": {"ref": "service:web"}, "agent_id": "agent-42"},
            )
            assert response.status_code == 200

            msg = json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT))
        finally:
            close_ws(ws)

    assert msg == {
        "type": "layout_op",
        "op": "open",
        "args": {"ref": "service:web"},
        "requester_agent_id": "agent-42",
    }


@pytest.mark.timeout(15)
def test_layout_broadcast_refresh_bypasses_mutex(app: Flask) -> None:
    """``refresh`` is read-only and never acquires the mutex."""
    client = app.test_client()
    with serve_app(app) as served:
        ws = open_ws(served, "/api/ws")
        try:
            json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT))
            json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT))

            response = client.post(
                "/api/layout/broadcast",
                json={"op": "refresh", "args": {"ref": "service:web"}, "agent_id": "agent-42"},
            )
            assert response.status_code == 200
            msg = json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT))
        finally:
            close_ws(ws)

    assert msg == {
        "type": "layout_op",
        "op": "refresh",
        "args": {"ref": "service:web"},
        "requester_agent_id": "agent-42",
    }


@pytest.mark.timeout(15)
def test_layout_broadcast_reload_system_interface_emits_ws_message(app: Flask) -> None:
    """``reload_system_interface`` broadcasts a layout_op so the shell reloads.

    This is the frontend-reveal trigger: the reload script POSTs this op and the
    dockview shell responds by reloading the whole top-level page. It carries no
    args and bypasses the mutex (read-only).
    """
    client = app.test_client()
    with serve_app(app) as served:
        ws = open_ws(served, "/api/ws")
        try:
            json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT))
            json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT))

            response = client.post(
                "/api/layout/broadcast",
                json={"op": "reload_system_interface", "args": {}, "agent_id": "agent-42"},
            )
            assert response.status_code == 200
            msg = json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT))
        finally:
            close_ws(ws)

    assert msg == {
        "type": "layout_op",
        "op": "reload_system_interface",
        "args": {},
        "requester_agent_id": "agent-42",
    }


@pytest.mark.timeout(15)
def test_layout_broadcast_open_terminal_allocates_panel_id_and_returns_ref(app: Flask) -> None:
    """``open service:terminal`` is the synchronous-ref-return path.

    The endpoint pre-mints the panel id (so the frontend uses it
    verbatim and the resulting tab is deterministically addressable as
    ``terminal:<hash>``), injects it into the broadcast args, and
    returns the ref in the HTTP response. Every other op leaves the
    args dict alone and returns just ``{ok: true}``.
    """
    client = app.test_client()
    with serve_app(app) as served:
        ws = open_ws(served, "/api/ws")
        try:
            json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT))
            json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT))

            response = client.post(
                "/api/layout/broadcast",
                json={"op": "open", "args": {"ref": "service:terminal"}, "agent_id": "agent-42"},
            )
            assert response.status_code == 200
            body = response.get_json()
            ref = body["ref"]
            assert ref.startswith("terminal:")

            msg = json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT))
        finally:
            close_ws(ws)

    assert msg["op"] == "open"
    assert msg["requester_agent_id"] == "agent-42"
    # The frontend must receive the same panel id the server returned
    # the ref for, or the script's printed ref would address nothing.
    panel_id = msg["args"]["panel_id"]
    assert panel_id.startswith("iframe-terminal-")
    assert msg["args"]["ref"] == "service:terminal"


def test_layout_broadcast_open_non_terminal_returns_no_ref(client: FlaskClient) -> None:
    """Non-terminal opens must NOT carry a ``ref`` in the response: the
    CLI uses presence-of-ref to decide whether to print to stdout, and a
    stray ref on a regular service open would mislead callers."""
    response = client.post(
        "/api/layout/broadcast",
        json={"op": "open", "args": {"ref": "service:web"}, "agent_id": "agent-42"},
    )
    assert response.status_code == 200
    assert "ref" not in response.get_json()


def test_layout_broadcast_rejects_non_loopback(client: FlaskClient) -> None:
    """The layout broadcast endpoint refuses non-loopback callers."""
    response = client.post(
        "/api/layout/broadcast",
        json={"op": "open", "args": {"ref": "service:web"}, "agent_id": "agent-42"},
        environ_base={"REMOTE_ADDR": "10.0.0.1"},
    )
    assert response.status_code == 403


def test_get_events_seeds_pending_tool_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hitting /api/agents/{id}/events for a Claude session with an unmatched tool_use
    seeds the AgentManager's transcript-derived signals so the activity indicator
    reads ``TOOL_RUNNING`` immediately.
    """
    agent_id = "agent-pending-tool"
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", agent_id)
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(tmp_path / "work"))

    state_dir = tmp_path / "agents" / agent_id
    state_dir.mkdir(parents=True)

    claude_config_dir = tmp_path / "claude_config"
    projects_dir = claude_config_dir / "projects" / "hash123"
    projects_dir.mkdir(parents=True)
    session_id = "test-session-id"
    session_file = projects_dir / f"{session_id}.jsonl"
    # An assistant message that includes a tool_use, with no matching tool_result.
    session_file.write_text(
        json.dumps(
            {
                "type": "assistant",
                "uuid": "uuid-1",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-6",
                    "content": [
                        {"type": "text", "text": "running a command"},
                        {"type": "tool_use", "id": "call_a", "name": "Bash", "input": {"command": "ls"}},
                    ],
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            }
        )
        + "\n"
    )
    (state_dir / "claude_session_id_history").write_text(f"{session_id}\n")

    broadcaster = WebSocketBroadcaster()
    manager = AgentManager.build(broadcaster)
    with manager._lock:
        manager._agents[agent_id] = AgentStateItem(
            id=agent_id,
            name="seed-agent",
            state="RUNNING",
            labels={},
            work_dir=str(tmp_path / "work"),
        )
    manager._ensure_activity_tracking(agent_id)

    app = create_application(agent_manager=manager)
    agent_info = AgentInfo(
        id=agent_id,
        name="seed-agent",
        state="RUNNING",
        agent_state_dir=state_dir,
        claude_config_dir=claude_config_dir,
    )

    try:
        test_client = app.test_client()
        with patch("imbue.system_interface.server._find_agent", return_value=agent_info):
            response = test_client.get(f"/api/agents/{agent_id}/events")
        assert response.status_code == 200

        # The watcher creation path seeds transcript-derived state
        # synchronously. Assert before ``stop()``, which clears these
        # caches alongside the marker watchers.
        with manager._lock:
            assert manager._has_unmatched_tool_use_by_agent[agent_id] is True
            assert manager._activity_state_by_agent[agent_id] == ActivityState.TOOL_RUNNING
    finally:
        manager.stop()


def test_layout_broadcast_rejects_unknown_op(client: FlaskClient) -> None:
    response = client.post(
        "/api/layout/broadcast",
        json={"op": "explode", "args": {}, "agent_id": "agent-42"},
    )
    assert response.status_code == 400
    assert "Unknown layout op" in response.get_json()["detail"]


def test_layout_broadcast_rejects_non_dict_args(client: FlaskClient) -> None:
    response = client.post(
        "/api/layout/broadcast",
        json={"op": "open", "args": ["not", "a", "dict"], "agent_id": "agent-42"},
    )
    assert response.status_code == 400


def test_layout_broadcast_rejects_null_args(client: FlaskClient) -> None:
    """``args: null`` must be a 400, not silently coerced into ``{}``.

    A previous implementation collapsed every falsy non-dict via ``or {}``,
    which let mutating ops broadcast empty payloads that the frontend
    handlers silently dropped.
    """
    response = client.post(
        "/api/layout/broadcast",
        json={"op": "close", "args": None, "agent_id": "agent-42"},
    )
    assert response.status_code == 400


def test_layout_broadcast_mutex_returns_409_with_holder_metadata(app: Flask) -> None:
    """While agent A holds the mutex, agent B's mutating op is rejected with 409."""
    # Pre-acquire the mutex on behalf of agent-a so the test's request
    # races against an active holder deterministically (no thread timing).
    mutex: LayoutMutex = state_of(app).layout_mutex
    held = mutex.try_acquire("agent-a", "move", {"ref": "service:web"})
    assert held is None

    client = app.test_client()
    response = client.post(
        "/api/layout/broadcast",
        json={"op": "split", "args": {"ref": "service:api"}, "agent_id": "agent-b"},
    )
    assert response.status_code == 409
    body = response.get_json()
    assert body["retry_after_ms"] > 0
    in_flight = body["in_flight"]
    assert in_flight["agent_id"] == "agent-a"
    assert in_flight["operation"] == "move"
    assert in_flight["args"] == {"ref": "service:web"}


def test_layout_broadcast_inspect_reads_layout_json(
    app: Flask, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``inspect`` returns a ref-resolved summary of the saved layout."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-42")
    layout_dir = tmp_path / "agents" / "agent-42" / "workspace_layout"
    layout_dir.mkdir(parents=True)
    (layout_dir / "layout.json").write_text(
        json.dumps(
            {
                "dockview": {
                    "panels": {
                        "panel-1": {"id": "panel-1", "title": "web"},
                        "panel-2": {"id": "panel-2", "title": "chat"},
                    },
                    "grid": {
                        "root": {
                            "type": "leaf",
                            "data": {"views": ["panel-1", "panel-2"], "activeView": "panel-1", "size": 1.0},
                        },
                    },
                },
                "panelParams": {
                    "panel-1": {"panelType": "iframe", "serviceName": "web"},
                    "panel-2": {"panelType": "chat", "chatAgentId": "agent-42"},
                },
            }
        )
    )

    client = app.test_client()
    response = client.post(
        "/api/layout/broadcast",
        json={"op": "inspect", "args": {}, "agent_id": "agent-42"},
    )
    assert response.status_code == 200
    payload = response.get_json()
    layout_summary = payload["layout"]
    refs = [p["ref"] for p in layout_summary["panels"]]
    assert "service:web" in refs


def test_layout_broadcast_list_includes_open_flag(app: Flask, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``list`` reads the saved layout to compute ``is_open`` per service."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-42")
    layout_dir = tmp_path / "agents" / "agent-42" / "workspace_layout"
    layout_dir.mkdir(parents=True)
    (layout_dir / "layout.json").write_text(
        json.dumps(
            {
                "dockview": {"panels": {"panel-1": {"id": "panel-1", "title": "web"}}},
                "panelParams": {"panel-1": {"panelType": "iframe", "serviceName": "web"}},
            }
        )
    )

    client = app.test_client()
    response = client.post(
        "/api/layout/broadcast",
        json={"op": "list", "args": {}, "agent_id": "agent-42"},
    )
    assert response.status_code == 200
    entries = response.get_json()["entries"]
    # We don't know what services the agent_manager seeded; assert the
    # endpoint shape and that ``is_open`` is bool-typed if any entry exists.
    for entry in entries:
        assert set(entry.keys()) == {"ref", "kind", "display_name", "is_open", "is_running"}
        assert isinstance(entry["is_open"], bool)


@pytest.mark.timeout(15)
def test_proto_agent_logs_endpoint_not_found_sends_error_and_closes(app: Flask) -> None:
    """When the proto-agent is missing, the endpoint sends a structured not-found message and closes."""
    with serve_app(app) as served:
        ws = open_ws(served, "/api/proto-agents/missing-agent/logs")
        try:
            payload = json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT))
        finally:
            close_ws(ws)
    assert payload == {"done": True, "success": False, "error": "Proto-agent not found"}


@pytest.mark.timeout(15)
def test_proto_agent_logs_endpoint_streams_messages_until_sentinel(app: Flask) -> None:
    """The endpoint forwards real log lines and closes when the queue yields ``None``."""
    log_queue: queue.Queue[str | None] = queue.Queue()
    log_queue.put(json.dumps({"line": "starting"}))
    log_queue.put(json.dumps({"line": "still going"}))
    log_queue.put(None)

    agent_manager: AgentManager = state_of(app).agent_manager
    agent_manager._log_queues["proto-1"] = log_queue

    with serve_app(app) as served:
        ws = open_ws(served, "/api/proto-agents/proto-1/logs")
        try:
            first = json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT))
            second = json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT))
        finally:
            close_ws(ws)

    assert first == {"line": "starting"}
    assert second == {"line": "still going"}


def test_stream_filtered_events_forwards_only_matching_events() -> None:
    """The shared stream loop yields only events that pass its predicate.

    This is the wiring behind Bug 2: the main stream forwards main-session
    events and drops subagent-session events, which share the same per-agent
    queue. A queued ``None`` ends the stream, keeping the test deterministic.
    """
    event_queues = AgentEventQueues()
    event_queue = event_queues.register("agent-1")

    # Subagent event first so a missing filter would forward it before the main one.
    event_queue.put({"event_id": "sub-evt", "session_id": "agent-sub"})
    event_queue.put({"event_id": "main-evt", "session_id": "main-1"})
    # Plugin/app events have no session_id and must still pass through.
    event_queue.put({"event_id": "no-session"})
    event_queue.put(None)

    def is_main_session_event(event: dict[str, object]) -> bool:
        session_id = event.get("session_id")
        return session_id is None or session_id == "main-1"

    frames = list(_stream_filtered_events("agent-1", event_queues, event_queue, is_main_session_event))
    forwarded_ids = [json.loads(frame[len("data: ") :])["event_id"] for frame in frames if frame.startswith("data: ")]

    assert forwarded_ids == ["main-evt", "no-session"]
    assert "sub-evt" not in forwarded_ids


def test_destroy_rejects_is_primary_agent(client: FlaskClient, app: Flask) -> None:
    """POST /api/agents/<id>/destroy returns 400 for the services agent.

    The frontend already hides agents carrying ``is_primary=true``; this
    server-side guard prevents direct callers (curl, scripted use, etc.)
    from accidentally tearing down the workspace.
    """
    agent_manager: AgentManager = state_of(app).agent_manager
    services_agent = AgentStateItem(
        id="services-1",
        name="system-services",
        state="RUNNING",
        labels={"is_primary": "true", "workspace": "my-ws"},
        work_dir="/mngr/code",
    )
    agent_manager._agents[services_agent.id] = services_agent

    response = client.post(f"/api/agents/{services_agent.id}/destroy")
    assert response.status_code == 400
    assert "is_primary" in response.get_json()["detail"]
    # The guard runs *before* the destroy subprocess, so the agent is still
    # present in the agent manager's state.
    assert services_agent.id in agent_manager._agents


def _register_agent(app: Flask, agent_id: str, name: str, state: str) -> None:
    """Insert an agent into the AgentManager's state for endpoint tests."""
    agent_manager: AgentManager = state_of(app).agent_manager
    agent_manager._agents[agent_id] = AgentStateItem(
        id=agent_id,
        name=name,
        state=state,
        labels={},
        work_dir="/code",
    )


def test_start_unknown_agent_returns_404(client: FlaskClient) -> None:
    """POST /api/agents/<id>/start returns 404 for an unknown agent."""
    response = client.post("/api/agents/nonexistent/start")
    assert response.status_code == 404


def test_start_invokes_in_process_start_with_agent_name(client: FlaskClient, app: Flask) -> None:
    """The endpoint delegates to the in-process ``start_agent`` keyed by name.

    Opening a terminal must go through the same in-process mngr start path that
    messaging an agent uses, so the two cannot diverge. The endpoint therefore
    calls ``start_agent(<name>)`` rather than shelling out to ``mngr start``.
    """
    _register_agent(app, "agent-running", "running-agent", "RUNNING")

    with patch("imbue.system_interface.server.start_agent") as mock_start:
        response = client.post("/api/agents/agent-running/start")

    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"
    mock_start.assert_called_once_with("running-agent")


def test_start_failure_returns_500(client: FlaskClient, app: Flask) -> None:
    """A failed start surfaces as a 500 carrying the mngr error message."""
    _register_agent(app, "agent-stopped", "stopped-agent", "STOPPED")

    with patch(
        "imbue.system_interface.server.start_agent",
        side_effect=AgentStartError("stopped-agent", "boom"),
    ):
        response = client.post("/api/agents/agent-stopped/start")

    assert response.status_code == 500
    assert "boom" in response.get_json()["detail"]


def test_destroy_argv_accepted_by_live_cli() -> None:
    """Confront the ``mngr destroy`` argv with the live ``imbue.mngr.main.cli``
    tree, so a vendor/mngr rename of that subcommand/flag fails here at merge
    time rather than only surfacing at runtime."""
    assert_mngr_argv_valid(_build_destroy_command("demo"))
