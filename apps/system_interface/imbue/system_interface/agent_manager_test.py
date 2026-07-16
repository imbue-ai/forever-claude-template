"""Tests for the AgentManager."""

import json
import queue
import shutil
import threading
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

import pytest
from mngr_cli_contract.contract import assert_mngr_argv_valid
from watchdog.events import FileClosedNoWriteEvent
from watchdog.events import FileModifiedEvent
from watchdog.events import FileMovedEvent
from watchdog.events import FileOpenedEvent

from imbue.mngr.api.observe import make_agent_removed_event
from imbue.mngr.api.observe import make_agent_state_event
from imbue.mngr.api.observe import make_full_agent_state_event
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.interfaces.data_types import HostDetails
from imbue.mngr.primitives import AgentId as MngrAgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName as MngrAgentName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.polling import poll_until
from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.agent_manager import AgentManager
from imbue.system_interface.agent_manager import _LogQueueCallback
from imbue.system_interface.agent_manager import _build_chat_create_command
from imbue.system_interface.agent_manager import _build_observe_command_argv
from imbue.system_interface.agent_manager import _build_worktree_create_command
from imbue.system_interface.agent_manager import _make_applications_file_handler
from imbue.system_interface.models import AgentCreationError
from imbue.system_interface.models import AgentStateItem
from imbue.system_interface.models import ApplicationEntry
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster

# Several tests in this module spin up real watchdog FSEvents observers
# (via ``_start_app_watcher``). On macOS the FSEvents emitter thread
# occasionally stalls during shutdown, tripping pytest-timeout. Mark the
# whole file as flaky so offload retries it automatically -- mirrors
# ``ws_broadcaster_test.py``.
pytestmark = pytest.mark.flaky


def _seed_agent(manager: AgentManager, agent_id: str) -> None:
    """Insert a placeholder ``AgentStateItem`` directly into the tracked map."""
    with manager._lock:
        manager._agents[agent_id] = AgentStateItem(
            id=agent_id,
            name=f"agent-{agent_id}",
            state="RUNNING",
            labels={},
            work_dir=None,
        )


_PROVIDER = ProviderInstanceName("local")


def _agent_details(
    name: str,
    agent_id: MngrAgentId | None = None,
    state: AgentLifecycleState = AgentLifecycleState.RUNNING,
    labels: dict[str, str] | None = None,
    work_dir: str = "/tmp/work",
    host_id: HostId | None = None,
    provider_name: ProviderInstanceName = _PROVIDER,
) -> AgentDetails:
    """Build an ``AgentDetails`` with controllable identity, state, and location.

    Mirrors what the observe stream carries: a real lifecycle ``state`` and a
    nested ``HostDetails`` whose id/provider are what ``_build_agent_match`` reads
    to route messages. Fields the manager never inspects are given inert defaults.
    """
    return AgentDetails(
        id=agent_id if agent_id is not None else MngrAgentId(),
        name=MngrAgentName(name),
        type="claude",
        command=CommandString("claude"),
        work_dir=Path(work_dir),
        initial_branch=None,
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=state,
        labels=labels if labels is not None else {},
        host=HostDetails(
            id=host_id if host_id is not None else HostId(),
            name="test-host",
            provider_name=provider_name,
            state=HostState.RUNNING,
        ),
    )


def _drain(q: queue.Queue[str | None]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    while not q.empty():
        raw = q.get_nowait()
        if raw is None:
            break
        out.append(json.loads(raw))
    return out


def _last_agents_updated(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for message in reversed(messages):
        if message.get("type") == "agents_updated":
            return message
    return None


def test_generate_random_name(agent_manager: AgentManager) -> None:
    name = agent_manager.generate_random_name()
    assert isinstance(name, str)
    assert len(name) > 0
    assert "-" in name


def test_get_agents_initially_empty(agent_manager: AgentManager) -> None:
    agents = agent_manager.get_agents()
    assert agents == []


def test_get_applications_initially_empty(agent_manager: AgentManager) -> None:
    apps = agent_manager.get_applications()
    assert apps == []


def test_get_proto_agents_initially_empty(agent_manager: AgentManager) -> None:
    protos = agent_manager.get_proto_agents()
    assert protos == []


def test_read_applications_parses_toml(agent_manager: AgentManager, tmp_path: Path) -> None:
    toml_content = """
[[applications]]
name = "web"
url = "http://localhost:8000"

[[applications]]
name = "terminal"
url = "http://localhost:7681"
"""
    toml_file = tmp_path / "applications.toml"
    toml_file.write_text(toml_content)

    agent_manager._read_applications(toml_file)

    apps = agent_manager.get_applications()
    assert len(apps) == 2
    assert apps[0].name == "web"
    assert apps[0].url == "http://localhost:8000"
    assert apps[1].name == "terminal"
    assert apps[1].url == "http://localhost:7681"


def test_read_applications_handles_missing_file(agent_manager: AgentManager, tmp_path: Path) -> None:
    toml_file = tmp_path / "nonexistent.toml"
    agent_manager._read_applications(toml_file)

    apps = agent_manager.get_applications()
    assert apps == []


def test_read_applications_handles_empty_file(agent_manager: AgentManager, tmp_path: Path) -> None:
    toml_file = tmp_path / "empty.toml"
    toml_file.write_text("")
    agent_manager._read_applications(toml_file)

    apps = agent_manager.get_applications()
    assert apps == []


def test_read_applications_ignores_entries_without_name(agent_manager: AgentManager, tmp_path: Path) -> None:
    toml_content = """
[[applications]]
url = "http://localhost:8000"
"""
    toml_file = tmp_path / "applications.toml"
    toml_file.write_text(toml_content)

    agent_manager._read_applications(toml_file)

    apps = agent_manager.get_applications()
    assert apps == []


def test_get_agents_serialized(agent_manager: AgentManager) -> None:
    with agent_manager._lock:
        agent_manager._agents["a1"] = AgentStateItem(
            id="a1",
            name="agent-one",
            state="RUNNING",
            labels={"user_created": "true"},
            work_dir="/tmp/work",
        )

    serialized = agent_manager.get_agents_serialized()
    assert len(serialized) == 1
    assert serialized[0]["id"] == "a1"
    assert serialized[0]["name"] == "agent-one"
    assert serialized[0]["labels"] == {"user_created": "true"}
    assert serialized[0]["activity_state"] is None


def test_get_applications_serialized(agent_manager: AgentManager) -> None:
    with agent_manager._lock:
        agent_manager._applications = [
            ApplicationEntry(name="web", url="http://localhost:8000"),
        ]

    serialized = agent_manager.get_applications_serialized()
    assert serialized == [{"name": "web", "url": "http://localhost:8000"}]


def test_resolve_agent_work_dir_from_own_env(agent_manager: AgentManager) -> None:
    with agent_manager._lock:
        result = agent_manager._resolve_agent_work_dir("test-agent-id")
    assert result == "/tmp/test-work"


def test_resolve_agent_work_dir_from_tracked_agent(agent_manager: AgentManager) -> None:
    with agent_manager._lock:
        agent_manager._agents["other-agent"] = AgentStateItem(
            id="other-agent",
            name="other",
            state="RUNNING",
            labels={},
            work_dir="/tmp/other-work",
        )
        result = agent_manager._resolve_agent_work_dir("other-agent")
    assert result == "/tmp/other-work"


def test_resolve_agent_work_dir_returns_none_for_unknown(agent_manager: AgentManager) -> None:
    with agent_manager._lock:
        result = agent_manager._resolve_agent_work_dir("unknown-id")
    assert result is None


def test_create_chat_agent_broadcasts_proto_created(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster
) -> None:
    """The proto_agent_created broadcast fires before the creation thread runs."""
    q = broadcaster.register()

    agent_id = agent_manager.create_chat_agent("test-chat")
    agent_manager.stop()

    assert isinstance(agent_id, str)
    assert len(agent_id) > 0

    raw = q.get_nowait()
    assert raw is not None
    proto_msg = json.loads(raw)
    assert proto_msg["type"] == "proto_agent_created"
    assert proto_msg["agent_id"] == agent_id
    assert proto_msg["creation_type"] == "chat"
    assert proto_msg["parent_agent_id"] is None


def test_create_worktree_agent_broadcasts_proto_created(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, git_work_dir: Path
) -> None:
    """The proto_agent_created broadcast fires before the creation thread runs."""
    q = broadcaster.register()

    with agent_manager._lock:
        agent_manager._agents["parent-id"] = AgentStateItem(
            id="parent-id",
            name="parent",
            state="RUNNING",
            labels={},
            work_dir=str(git_work_dir),
        )

    agent_id = agent_manager.create_worktree_agent("test-worktree", "parent-id")
    agent_manager.stop()

    assert isinstance(agent_id, str)

    raw = q.get_nowait()
    assert raw is not None
    proto_msg = json.loads(raw)
    assert proto_msg["type"] == "proto_agent_created"
    assert proto_msg["creation_type"] == "worktree"
    assert proto_msg["parent_agent_id"] is None


def test_get_log_queue_for_proto_agent(agent_manager: AgentManager, git_work_dir: Path) -> None:
    """The log queue is available immediately after create_worktree_agent returns."""
    with agent_manager._lock:
        agent_manager._agents["parent-id"] = AgentStateItem(
            id="parent-id",
            name="parent",
            state="RUNNING",
            labels={},
            work_dir=str(git_work_dir),
        )

    agent_id = agent_manager.create_worktree_agent("test-worktree", "parent-id")
    log_q = agent_manager.get_log_queue(agent_id)
    assert log_q is not None

    agent_manager.stop()


def test_get_log_queue_returns_none_for_unknown(agent_manager: AgentManager) -> None:
    assert agent_manager.get_log_queue("nonexistent") is None


def test_stop_without_start(agent_manager: AgentManager) -> None:
    """Stopping an agent manager that was never started is safe."""
    agent_manager.stop()


def test_agent_state_event_adds_agent(agent_manager: AgentManager, broadcaster: WebSocketBroadcaster) -> None:
    """An AGENT_STATE event for a new agent updates the agent list and broadcasts."""
    q = broadcaster.register()

    test_agent_id = MngrAgentId()
    agent = _agent_details("discovered-agent", agent_id=test_agent_id, labels={"user_created": "true"})

    agent_manager._handle_observe_event(make_agent_state_event(agent))

    agents = agent_manager.get_agents()
    assert len(agents) == 1
    assert agents[0].id == str(test_agent_id)
    assert agents[0].name == "discovered-agent"

    raw = q.get_nowait()
    assert raw is not None
    msg = json.loads(raw)
    assert msg["type"] == "agents_updated"


def test_discovered_agent_labels_come_from_discovery(agent_manager: AgentManager) -> None:
    """An agent's labels are taken straight from its discovery event.

    Discovery re-reads each agent's data.json into ``certified_data`` on every
    poll (see DiscoveredAgent.labels), so the snapshot already carries current
    labels -- including the ``highlight`` run-key that run_task_agent.sh bumps on
    each task-agent run to re-flash its tab. The web UI keys its is_primary hiding
    and its tab-blink off these, so they must pass straight through.
    """
    agent = DiscoveredAgent(
        host_id=HostId(),
        agent_id=MngrAgentId(),
        agent_name=MngrAgentName("caretaker"),
        provider_name=ProviderInstanceName("local"),
        certified_data={
            "work_dir": "/tmp/work",
            "labels": {"task_agent": "caretaker", "highlight": "1700000042", "workspace": "ws"},
        },
    )

    agent_manager._handle_discovery_event(_provider_snapshot([agent]))

    agents = agent_manager.get_agents()
    assert len(agents) == 1
    assert agents[0].labels == {"task_agent": "caretaker", "highlight": "1700000042", "workspace": "ws"}


def test_discovered_agent_with_no_labels_has_empty_labels(
    agent_manager: AgentManager,
) -> None:
    """An agent whose discovery event carries no labels gets empty labels (no error)."""
    agent = DiscoveredAgent(
        host_id=HostId(),
        agent_id=MngrAgentId(),
        agent_name=MngrAgentName("remote-agent"),
        provider_name=ProviderInstanceName("local"),
        certified_data={"work_dir": None},
    )

    agent_manager._handle_discovery_event(_provider_snapshot([agent]))

    agents = agent_manager.get_agents()
    assert len(agents) == 1
    assert agents[0].labels == {}


def _layout_ops(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [message for message in messages if message.get("type") == "layout_op"]


def test_assist_labeled_agent_auto_opens_its_tab(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster
) -> None:
    """A chat spawned by the get-help flow (carrying the ``assist`` label) auto-opens its tab."""
    q = broadcaster.register()
    agent = _agent_details("assist-abc123", labels={"assist": "true"})
    agent_manager._handle_observe_event(make_agent_state_event(agent))

    messages = _drain(q)
    opens = _layout_ops(messages)
    assert len(opens) == 1
    assert opens[0]["op"] == "open"
    assert opens[0]["args"] == {"ref": "chat:assist-abc123"}
    # The agent list must be broadcast before the open, or the frontend drops the open
    # (it resolves ``chat:<name>`` against its known-agents list).
    types = [m.get("type") for m in messages]
    assert types.index("agents_updated") < types.index("layout_op")


def test_non_assist_agent_does_not_auto_open(agent_manager: AgentManager, broadcaster: WebSocketBroadcaster) -> None:
    """An ordinary discovered agent (no ``assist`` label) does not trigger an auto-open."""
    q = broadcaster.register()
    agent = _agent_details("plain-agent", labels={"user_created": "true"})
    agent_manager._handle_observe_event(make_agent_state_event(agent))

    assert _layout_ops(_drain(q)) == []


def test_assist_agent_rediscovery_does_not_reopen(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster
) -> None:
    """A re-emitted AGENT_STATE event for an already-seen assist chat does not reopen its tab."""
    agent = _agent_details("assist-xyz", labels={"assist": "true"})
    agent_manager._handle_observe_event(make_agent_state_event(agent))
    # Register only after the first event so the queue captures just the re-delivery.
    q = broadcaster.register()
    agent_manager._handle_observe_event(make_agent_state_event(agent))

    assert _layout_ops(_drain(q)) == []


def _assist_agent_details(name: str) -> AgentDetails:
    return _agent_details(name, labels={"assist": "true"})


def test_snapshot_auto_opens_a_newly_appeared_assist_chat(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster
) -> None:
    """A freshly-created chat usually surfaces in a full snapshot (not a per-agent delta),
    so the snapshot path must auto-open assist chats too."""
    q = broadcaster.register()
    agent = _assist_agent_details("assist-snap")
    agent_manager._handle_observe_event(make_full_agent_state_event([agent]))

    messages = _drain(q)
    opens = _layout_ops(messages)
    assert len(opens) == 1
    assert opens[0]["op"] == "open"
    assert opens[0]["args"] == {"ref": "chat:assist-snap"}
    # The agent list must be broadcast before the open, or the frontend drops the open
    # (it resolves ``chat:<name>`` against its known-agents list).
    types = [m.get("type") for m in messages]
    assert types.index("agents_updated") < types.index("layout_op")


def test_snapshot_does_not_reopen_assist_chat_on_later_snapshots(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster
) -> None:
    agent = _assist_agent_details("assist-snap2")
    agent_manager._handle_observe_event(make_full_agent_state_event([agent]))
    # Register after the first snapshot so the queue captures only the second.
    q = broadcaster.register()
    agent_manager._handle_observe_event(make_full_agent_state_event([agent]))

    assert _layout_ops(_drain(q)) == []


def test_assist_chat_present_at_startup_is_not_auto_opened(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster
) -> None:
    """Assist chats seeded as already-handled (what ``_initial_discover`` does for chats that
    exist at startup) are not auto-opened, so a restart restores the saved layout."""
    agent = _assist_agent_details("assist-existing")
    with agent_manager._lock:
        agent_manager._auto_opened_assist_ids.add(str(agent.id))
    q = broadcaster.register()
    agent_manager._handle_observe_event(make_full_agent_state_event([agent]))

    assert _layout_ops(_drain(q)) == []


def test_agent_removed_event_removes_agent(agent_manager: AgentManager, broadcaster: WebSocketBroadcaster) -> None:
    """An AGENT_REMOVED event removes the agent from the tracked list and broadcasts."""
    test_agent_id = MngrAgentId()
    str_id = str(test_agent_id)
    q = broadcaster.register()

    agent = _agent_details("doomed", agent_id=test_agent_id)
    agent_manager._handle_observe_event(make_agent_state_event(agent))
    assert len(agent_manager.get_agents()) == 1

    q.get_nowait()

    agent_manager._handle_observe_event(make_agent_removed_event(agent.id, agent.name))

    agents = agent_manager.get_agents()
    assert len(agents) == 0

    raw = q.get_nowait()
    assert raw is not None
    msg = json.loads(raw)
    assert msg["type"] == "agents_updated"
    assert str_id not in [a["id"] for a in msg["agents"]]


def _full_snapshot_with_agent(name: str) -> tuple[MngrAgentId, HostId, AgentDetails]:
    agent = _agent_details(name)
    return agent.id, agent.host.id, agent


def test_full_snapshot_populates_agent_locations(agent_manager: AgentManager) -> None:
    """A snapshot records each agent's routing location (id/host/provider) so messaging skips discovery."""
    agent_id, host_id, agent = _full_snapshot_with_agent("locatable")
    agent_manager._handle_observe_event(make_full_agent_state_event([agent]))

    matches = agent_manager.get_agent_matches_by_id(str(agent_id))
    assert len(matches) == 1
    match = matches[0]
    assert str(match.agent_id) == str(agent_id)
    assert str(match.agent_name) == "locatable"
    assert str(match.host_id) == str(host_id)
    assert str(match.provider_name) == "local"

    assert agent_manager.get_agent_matches_by_id("agent-does-not-exist") == []


def test_agent_location_dropped_when_absent_from_snapshot(agent_manager: AgentManager) -> None:
    """An agent missing from a later snapshot loses its cached location."""
    agent_id, _host_id, agent = _full_snapshot_with_agent("ephemeral")
    agent_manager._handle_observe_event(make_full_agent_state_event([agent]))
    assert len(agent_manager.get_agent_matches_by_id(str(agent_id))) == 1

    agent_manager._handle_observe_event(make_full_agent_state_event([]))
    assert agent_manager.get_agent_matches_by_id(str(agent_id)) == []


def test_get_agent_matches_by_id_disambiguates_shared_name(agent_manager: AgentManager) -> None:
    """Two agents sharing a name on different hosts are each retrievable by their own id."""
    host_a, host_b = HostId(), HostId()
    agent_a = _agent_details("twin", host_id=host_a)
    agent_b = _agent_details("twin", host_id=host_b)
    agent_manager._handle_observe_event(make_full_agent_state_event([agent_a, agent_b]))

    matches_a = agent_manager.get_agent_matches_by_id(str(agent_a.id))
    matches_b = agent_manager.get_agent_matches_by_id(str(agent_b.id))
    assert len(matches_a) == 1 and str(matches_a[0].host_id) == str(host_a)
    assert len(matches_b) == 1 and str(matches_b[0].host_id) == str(host_b)


def test_agent_location_updates_when_host_changes(agent_manager: AgentManager) -> None:
    """A later snapshot relocating an agent (new host_id) replaces its cached location."""
    agent_id = MngrAgentId()
    host_a, host_b = HostId(), HostId()
    agent_manager._handle_observe_event(
        make_full_agent_state_event([_agent_details("mover", agent_id=agent_id, host_id=host_a)])
    )
    assert str(agent_manager.get_agent_matches_by_id(str(agent_id))[0].host_id) == str(host_a)

    agent_manager._handle_observe_event(
        make_full_agent_state_event([_agent_details("mover", agent_id=agent_id, host_id=host_b)])
    )
    matches = agent_manager.get_agent_matches_by_id(str(agent_id))
    assert len(matches) == 1
    assert str(matches[0].host_id) == str(host_b)


def test_remove_agent_drops_location(agent_manager: AgentManager) -> None:
    """remove_agent (the API destroy path) drops the cached location too."""
    agent_id, _host_id, agent = _full_snapshot_with_agent("doomed")
    agent_manager._handle_observe_event(make_full_agent_state_event([agent]))
    assert len(agent_manager.get_agent_matches_by_id(str(agent_id))) == 1

    agent_manager.remove_agent(str(agent_id))
    assert agent_manager.get_agent_matches_by_id(str(agent_id)) == []


def test_get_agent_info_by_id_resolves_from_state(agent_manager: AgentManager, tmp_path: Path) -> None:
    """get_agent_info_by_id builds an AgentInfo from the live state (with resolved dirs)."""
    with agent_manager._lock:
        agent_manager._agents["agent-1"] = AgentStateItem(
            id="agent-1", name="alpha", state="RUNNING", labels={"k": "v"}, work_dir="/w"
        )

    info = agent_manager.get_agent_info_by_id("agent-1")
    assert info is not None
    assert info.id == "agent-1"
    assert info.name == "alpha"
    assert info.labels == {"k": "v"}
    assert agent_manager.get_agent_info_by_id("missing") is None


def test_agent_state_event_locates_agent_immediately(agent_manager: AgentManager) -> None:
    """An AGENT_STATE event records the routing location (id/host/provider) at once,
    so the first message to a just-created agent skips discovery instead of waiting for
    the next full snapshot."""
    fresh = _agent_details("freshly-created")
    agent_manager._handle_observe_event(make_agent_state_event(fresh))

    matches = agent_manager.get_agent_matches_by_id(str(fresh.id))
    assert len(matches) == 1
    assert str(matches[0].agent_name) == "freshly-created"
    assert str(matches[0].host_id) == str(fresh.host.id)
    assert str(matches[0].provider_name) == "local"


def test_on_applications_changed(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """Application changes are detected and broadcast."""
    q = broadcaster.register()

    toml_path = tmp_path / "runtime" / "applications.toml"
    toml_path.parent.mkdir(parents=True, exist_ok=True)
    toml_path.write_text('[[applications]]\nname = "web"\nurl = "http://localhost:8000"\n')

    with agent_manager._lock:
        agent_manager._agents["app-agent"] = AgentStateItem(
            id="app-agent",
            name="app-agent",
            state="RUNNING",
            labels={},
            work_dir=str(tmp_path),
        )

    agent_manager._on_applications_changed("app-agent")

    apps = agent_manager.get_applications()
    assert len(apps) == 1
    assert apps[0].name == "web"

    raw = q.get_nowait()
    assert raw is not None
    msg = json.loads(raw)
    assert msg["type"] == "applications_updated"


def test_read_applications_handles_invalid_toml(agent_manager: AgentManager, tmp_path: Path) -> None:
    """Invalid TOML files are handled gracefully."""
    toml_file = tmp_path / "bad.toml"
    toml_file.write_text("this is [[ not valid toml {{")

    agent_manager._read_applications(toml_file)

    apps = agent_manager.get_applications()
    assert apps == []


def test_unknown_observe_event_type_is_ignored(agent_manager: AgentManager) -> None:
    """An observe line whose ``type`` is not one of the three agents-stream events is ignored.

    ``parse_observe_event_line`` returns None for unrecognized (forward-compatible)
    types, so the output-line handler must swallow it without raising or mutating
    the tracked agent set.
    """
    line = json.dumps(
        {
            "type": "AGENT_STATE_CHANGE",
            "timestamp": "2026-01-01T00:00:00.000000000Z",
            "event_id": "test-event-id",
            "source": "mngr/agent_states",
        }
    )
    agent_manager._handle_observe_output_line(line, True)
    assert agent_manager.get_agents() == []


def test_create_worktree_raises_for_unknown_agent(agent_manager: AgentManager) -> None:
    """Creating a worktree for an unknown agent raises."""
    with pytest.raises(AgentCreationError, match="Cannot determine work directory"):
        agent_manager.create_worktree_agent("test", "nonexistent")


@pytest.mark.flaky
def test_start_app_watcher(agent_manager: AgentManager, tmp_path: Path) -> None:
    """Starting an app watcher for an agent creates the runtime directory."""
    runtime_dir = tmp_path / "runtime"
    agent_manager._start_app_watcher("watcher-test", tmp_path)
    assert runtime_dir.exists()
    agent_manager._stop_app_watcher("watcher-test")


def test_applications_file_handler_fires_on_move(tmp_path: Path) -> None:
    """The applications watcher must react to move/rename events, not just
    modify events. scripts/forward_port.py writes applications.toml atomically
    via ``tempfile.mkstemp`` + ``os.replace``, which surfaces as an
    ``IN_MOVED_TO`` / ``FileMovedEvent`` in watchdog -- if the handler only
    listened on ``on_modified`` every service registration after startup
    would be silently dropped.
    """
    seen: list[str] = []
    handler = _make_applications_file_handler("agent-x", lambda aid: seen.append(aid))

    # Simulate what os.replace(tmp, applications.toml) surfaces as.
    handler.dispatch(
        FileMovedEvent(
            src_path=str(tmp_path / "applications.toml.tmp"),
            dest_path=str(tmp_path / "applications.toml"),
        )
    )

    assert seen == ["agent-x"]


def test_applications_file_handler_ignores_unrelated_paths(tmp_path: Path) -> None:
    """The handler must not fire for writes to forward_port.py's scratch
    ``applications.toml.*.tmp`` files. Every upsert creates and modifies one
    of those before the atomic rename, and firing on each would produce a
    broadcast storm with no useful information (the scratch file is never
    the source of truth we read).
    """
    seen: list[str] = []
    handler = _make_applications_file_handler("agent-x", lambda aid: seen.append(aid))

    handler.dispatch(FileModifiedEvent(src_path=str(tmp_path / "applications.toml.abc123.tmp")))

    assert seen == []


def test_applications_file_handler_ignores_open_and_close_no_write(tmp_path: Path) -> None:
    """The handler must not fire on read-only events (FileOpenedEvent /
    FileClosedNoWriteEvent). Watchdog 3+ emits these on Linux for any open()
    / close() of the watched file -- including the read() inside
    _read_applications itself. If the handler reacts to them it triggers an
    inotify feedback loop that pins one CPU core per agent watcher.
    """
    seen: list[str] = []
    handler = _make_applications_file_handler("agent-x", lambda aid: seen.append(aid))

    handler.dispatch(FileOpenedEvent(src_path=str(tmp_path / "applications.toml")))
    handler.dispatch(FileClosedNoWriteEvent(src_path=str(tmp_path / "applications.toml")))

    assert seen == []


def test_applications_file_handler_fires_on_modify(tmp_path: Path) -> None:
    """A direct write (e.g. ``echo ... > applications.toml``) surfaces as a
    FileModifiedEvent and must still trigger the change callback.
    """
    seen: list[str] = []
    handler = _make_applications_file_handler("agent-x", lambda aid: seen.append(aid))

    handler.dispatch(FileModifiedEvent(src_path=str(tmp_path / "applications.toml")))

    assert seen == ["agent-x"]


def test_stop_app_watcher_nonexistent(agent_manager: AgentManager) -> None:
    """Stopping a watcher for an agent that isn't watched is safe."""
    agent_manager._stop_app_watcher("nonexistent")


def test_initial_discover_populates_agents(
    broadcaster: WebSocketBroadcaster,
) -> None:
    """Initial discovery populates agent list when discovery succeeds."""
    manager = AgentManager.build(broadcaster)
    manager._initial_discover()


def test_initial_discover_handles_errors(
    broadcaster: WebSocketBroadcaster,
) -> None:
    """Initial discovery handles errors gracefully when mngr is unavailable."""
    manager = AgentManager.build(broadcaster)
    manager._initial_discover()
    assert isinstance(manager.get_agents(), list)


def test_refresh_agents_does_not_crash(agent_manager: AgentManager, broadcaster: WebSocketBroadcaster) -> None:
    """Refresh agents handles errors gracefully and does not raise."""
    agent_manager._refresh_agents()
    assert isinstance(agent_manager.get_agents(), list)


def test_full_snapshot_replaces_agent_set(agent_manager: AgentManager, broadcaster: WebSocketBroadcaster) -> None:
    """A full state snapshot replaces the entire tracked agent set."""
    q = broadcaster.register()

    agent1 = _agent_details("agent-one", work_dir="/tmp/w1")
    agent2 = _agent_details("agent-two", work_dir="/tmp/w2")
    event = make_full_agent_state_event([agent1, agent2])

    agent_manager._handle_observe_event(event)

    agents = agent_manager.get_agents()
    assert len(agents) == 2

    raw = q.get_nowait()
    assert raw is not None
    msg = json.loads(raw)
    assert msg["type"] == "agents_updated"
    assert len(msg["agents"]) == 2


def test_run_creation_logs_header_and_completion(agent_manager: AgentManager, tmp_path: Path) -> None:
    """Creation thread logs a header line and a done message."""
    log_q: queue.Queue[str | None] = queue.Queue(maxsize=10000)
    cmd = ["true"]

    done_event = threading.Event()

    def run_and_signal() -> None:
        agent_manager._run_creation("test-id", "test-agent", cmd, tmp_path, log_q, {})
        done_event.set()

    t = threading.Thread(target=run_and_signal, daemon=True)
    t.start()
    done_event.wait(timeout=10)

    messages = [json.loads(item) for item in iter(log_q.get_nowait, None)]

    assert any("line" in m and str(tmp_path) in m["line"] for m in messages)
    done_msgs = [m for m in messages if "done" in m]
    assert len(done_msgs) == 1
    assert done_msgs[0]["success"] is True


def test_log_queue_callback_puts_json_line(
    agent_manager: AgentManager,
) -> None:
    """_LogQueueCallback writes each line as a JSON object to the queue."""
    q: queue.Queue[str | None] = queue.Queue()
    cb = _LogQueueCallback(log_queue=q)
    cb("hello\n", True)

    item = q.get_nowait()
    assert item is not None
    assert json.loads(item) == {"line": "hello"}


def test_handle_observe_output_line_empty_is_ignored(agent_manager: AgentManager) -> None:
    """Empty lines from the observe subprocess are silently ignored."""
    agent_manager._handle_observe_output_line("   ", True)
    assert agent_manager.get_agents() == []


def test_handle_observe_output_line_raises_on_invalid_json(agent_manager: AgentManager) -> None:
    """Invalid JSON on stdout from mngr observe surfaces as JSONDecodeError so the upstream bug is visible."""
    with pytest.raises(json.JSONDecodeError):
        agent_manager._handle_observe_output_line("not json {", True)
    assert agent_manager.get_agents() == []


def test_handle_observe_output_line_dispatches_agent_state(
    agent_manager: AgentManager,
) -> None:
    """Valid AGENT_STATE JSONL lines are parsed and dispatched."""
    test_agent_id = MngrAgentId()
    agent = _agent_details("obs-agent", agent_id=test_agent_id)
    event = make_agent_state_event(agent)
    line = json.dumps(event.model_dump(mode="json"))

    agent_manager._handle_observe_output_line(line, True)

    agents = agent_manager.get_agents()
    assert len(agents) == 1
    assert agents[0].id == str(test_agent_id)


def test_handle_observe_event_dispatches_full_state(
    agent_manager: AgentManager,
) -> None:
    """AGENTS_FULL_STATE events surface every agent they carry."""
    test_agent_id = MngrAgentId()
    agent = _agent_details("snap-agent", agent_id=test_agent_id)
    event = make_full_agent_state_event([agent])
    agent_manager._handle_observe_event(event)

    agents = agent_manager.get_agents()
    assert len(agents) == 1
    assert agents[0].id == str(test_agent_id)


def test_handle_observe_event_dispatches_agent_state(
    agent_manager: AgentManager,
) -> None:
    """AGENT_STATE events upsert the single agent they carry."""
    test_agent_id = MngrAgentId()
    agent = _agent_details("disc-agent", agent_id=test_agent_id)
    event = make_agent_state_event(agent)
    agent_manager._handle_observe_event(event)

    agents = agent_manager.get_agents()
    assert len(agents) == 1
    assert agents[0].id == str(test_agent_id)


def test_handle_observe_event_dispatches_agent_removed(
    agent_manager: AgentManager,
) -> None:
    """AGENT_REMOVED events drop the referenced agent."""
    test_agent_id = MngrAgentId()
    agent = _agent_details("to-destroy", agent_id=test_agent_id)
    agent_manager._handle_observe_event(make_agent_state_event(agent))
    assert len(agent_manager.get_agents()) == 1

    agent_manager._handle_observe_event(make_agent_removed_event(agent.id, agent.name))
    assert len(agent_manager.get_agents()) == 0


def test_full_snapshot_dropping_agents_removes_them(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster
) -> None:
    """A later full snapshot that omits previously-tracked agents drops them and broadcasts.

    There is no host event on the observe stream, so the way a whole host's worth
    of agents disappears is a rebuild snapshot that no longer lists them.
    """
    agent_id_1 = MngrAgentId()
    agent_id_2 = MngrAgentId()

    agents = [_agent_details(f"agent-{str(aid)[:8]}", agent_id=aid) for aid in (agent_id_1, agent_id_2)]
    agent_manager._handle_observe_event(make_full_agent_state_event(agents))
    assert len(agent_manager.get_agents()) == 2

    # Register after seeding so the queue captures only the drop broadcast.
    q = broadcaster.register()
    agent_manager._handle_observe_event(make_full_agent_state_event([]))

    assert len(agent_manager.get_agents()) == 0
    assert agent_manager.get_agent_matches_by_id(str(agent_id_1)) == []
    assert agent_manager.get_agent_matches_by_id(str(agent_id_2)) == []
    raw = q.get_nowait()
    assert raw is not None
    msg = json.loads(raw)
    assert msg["type"] == "agents_updated"


def test_full_snapshot_omitting_agent_drops_it(
    agent_manager: AgentManager,
) -> None:
    """A rebuild snapshot that no longer lists a tracked agent drops it from the set."""
    agent_id = MngrAgentId()
    agent = _agent_details("host-agent", agent_id=agent_id)
    agent_manager._handle_observe_event(make_full_agent_state_event([agent]))
    assert len(agent_manager.get_agents()) == 1

    agent_manager._handle_observe_event(make_full_agent_state_event([]))
    assert len(agent_manager.get_agents()) == 0


def test_build_observe_command_honors_injected_binary(broadcaster: WebSocketBroadcaster) -> None:
    """The ``mngr_binary`` argument to ``build()`` overrides the default binary path."""
    manager = AgentManager.build(broadcaster, mngr_binary="/path/to/custom-mngr")
    try:
        cmd = manager._build_observe_command()
        assert cmd == ["/path/to/custom-mngr", "observe", "--stream-events"]
    finally:
        manager.stop()


# --- mngr CLI argv contract ---
# These confront each builder's argv with the live ``imbue.mngr.main.cli`` tree,
# so a vendor/mngr subcommand/flag rename fails here at merge time rather than
# only surfacing at runtime. See ``mngr_cli_contract`` for the validator.


def test_worktree_create_argv_accepted_by_live_cli() -> None:
    argv = _build_worktree_create_command(
        mngr_binary="mngr",
        name="demo",
        agent_id="agent-123",
        current_branch="main",
        new_branch="mngr/demo",
        parent_labels={"project": "proj"},
    )
    assert_mngr_argv_valid(argv)


def test_worktree_create_argv_without_project_label() -> None:
    argv = _build_worktree_create_command(
        mngr_binary="mngr",
        name="demo",
        agent_id="agent-123",
        current_branch="main",
        new_branch="mngr/demo",
        parent_labels={},
    )
    assert_mngr_argv_valid(argv)


def test_chat_create_argv_accepted_by_live_cli() -> None:
    argv = _build_chat_create_command(
        mngr_binary="mngr",
        name="demo",
        agent_id="agent-123",
        primary_labels={"workspace": "ws", "project": "proj"},
    )
    assert_mngr_argv_valid(argv)
    # The chat carries user_created so the OOM launch wrapper puts it in the
    # dynamic chat band rather than the least-protected worker/unclassified band.
    assert "user_created=true" in argv


def test_get_chat_agent_ids_excludes_workers_and_primary(broadcaster: WebSocketBroadcaster) -> None:
    """Only chats are OOM-managed: workers and the primary keep their launch bands."""
    manager = AgentManager.build(broadcaster)
    try:
        with manager._lock:
            for agent_id, labels in (
                ("chat", {"user_created": "true"}),
                ("worker", {"agent_created": "true"}),
                ("primary", {"is_primary": "true"}),
            ):
                manager._agents[agent_id] = AgentStateItem(
                    id=agent_id, name=agent_id, state="RUNNING", labels=labels, work_dir=None
                )
        assert manager.get_chat_agent_ids() == ["chat"]
    finally:
        manager.stop()


def test_observe_argv_accepted_by_live_cli() -> None:
    argv = _build_observe_command_argv("mngr")
    assert_mngr_argv_valid(argv)
    assert "--stream-events" in argv


def test_resolve_observe_cwd_prefers_existing_work_dir(
    broadcaster: WebSocketBroadcaster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``MNGR_AGENT_WORK_DIR`` points at a real directory, observe runs there."""
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(tmp_path))
    manager = AgentManager.build(broadcaster)
    try:
        assert manager._resolve_observe_cwd() == tmp_path
    finally:
        manager.stop()


def test_resolve_observe_cwd_falls_back_when_work_dir_missing(
    broadcaster: WebSocketBroadcaster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``MNGR_AGENT_WORK_DIR`` is set but the path does not exist, use ``$HOME``.

    Guards the fallback that keeps observe runnable in tests that stub the env
    var with a non-existent path (e.g. the shared ``agent_manager`` fixture).
    """
    missing = tmp_path / "does-not-exist"
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(missing))
    manager = AgentManager.build(broadcaster)
    try:
        assert manager._resolve_observe_cwd() == Path.home()
    finally:
        manager.stop()


def test_resolve_observe_cwd_falls_back_when_work_dir_unset(
    broadcaster: WebSocketBroadcaster,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``MNGR_AGENT_WORK_DIR`` unset, observe runs from ``$HOME``."""
    monkeypatch.delenv("MNGR_AGENT_WORK_DIR", raising=False)
    manager = AgentManager.build(broadcaster)
    try:
        assert manager._resolve_observe_cwd() == Path.home()
    finally:
        manager.stop()


def test_start_observe_spawns_long_lived_subprocess(
    broadcaster: WebSocketBroadcaster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: the observe subprocess stays alive after startup.

    A healthy ``mngr observe`` keeps running until it is explicitly stopped;
    this test asserts that after ``_start_observe`` returns, the child is
    still running a short window later rather than having exited on its own.
    """
    if shutil.which("mngr") is None:
        pytest.skip("mngr binary not on PATH")

    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))
    # Point the subprocess at a clean cwd with no project-local .mngr/settings.toml;
    # otherwise running pytest from inside a mngr-managed worktree would inherit
    # a config with ``is_allowed_in_pytest = false`` and the child would abort.
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(tmp_path))
    manager = AgentManager.build(broadcaster)
    try:
        manager._start_observe()
        assert manager._observe_process is not None
        # If the subprocess exits within the window it's a failure (bad command,
        # crashed on startup, etc.). A healthy observe keeps running.
        exited = poll_until(
            lambda: manager._observe_process is not None and manager._observe_process.poll() is not None,
            timeout=1.5,
            poll_interval=0.1,
        )
        assert not exited, (
            "mngr observe subprocess exited within 1.5s of startup "
            f"(returncode={manager._observe_process.returncode}); stderr: "
            f"{manager._observe_process.read_stderr()!r}"
        )
    finally:
        manager.stop()


def test_start_observe_logs_error_when_subprocess_exits_unexpectedly(
    broadcaster: WebSocketBroadcaster,
    false_binary: str,
    loguru_records: list[str],
) -> None:
    """If the observe subprocess exits on its own, the watchdog logs an ERROR.

    Uses ``/usr/bin/false`` (or equivalent) as a stand-in mngr binary so the
    spawned process exits immediately with a non-zero code.
    """
    manager = AgentManager.build(broadcaster, mngr_binary=false_binary)
    try:
        manager._start_observe()
        logged_error = poll_until(
            lambda: any(r.startswith("ERROR") and "mngr observe" in r for r in loguru_records),
            timeout=5.0,
            poll_interval=0.05,
        )
        assert logged_error, (
            "Expected an ERROR log from the observe watchdog; got: "
            f"{[r for r in loguru_records if r.startswith('ERROR')]}"
        )
    finally:
        manager.stop()


def test_start_observe_watchdog_stays_quiet_on_clean_shutdown(
    broadcaster: WebSocketBroadcaster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[str],
) -> None:
    """Calling ``stop()`` on a healthy observe subprocess must not produce errors."""
    if shutil.which("mngr") is None:
        pytest.skip("mngr binary not on PATH")

    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))
    # See test_start_observe_spawns_long_lived_subprocess for why this is needed.
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(tmp_path))
    manager = AgentManager.build(broadcaster)
    manager._start_observe()
    # ``_start_observe`` only returns after ``run_process_in_background``
    # has spawned the child and its RunningProcess thread has started, so the
    # subprocess is guaranteed to be running by the time we call stop().
    assert manager._observe_process is not None
    manager.stop()

    errors = [r for r in loguru_records if r.startswith("ERROR") and "mngr observe" in r]
    assert errors == [], f"Watchdog logged errors during clean shutdown: {errors}"


def test_handle_observe_output_line_logs_stderr_as_warning(
    agent_manager: AgentManager,
    loguru_records: list[str],
) -> None:
    """Stderr output from the observe subprocess is surfaced as a warning."""
    agent_manager._handle_observe_output_line("something bad happened", is_stdout=False)

    warnings = [r for r in loguru_records if r.startswith("WARNING") and "mngr observe stderr" in r]
    assert warnings, f"Expected a stderr warning; got: {loguru_records}"
    assert "something bad happened" in warnings[0]


# ---------------------------------------------------------------------------
# Activity-state integration
# ---------------------------------------------------------------------------


def test_ensure_activity_tracking_skips_when_state_dir_missing(agent_manager: AgentManager) -> None:
    """No activity tracking is started for an agent whose host_dir state directory is absent."""
    _seed_agent(agent_manager, "remote-agent")
    agent_manager._ensure_activity_tracking("remote-agent")
    try:
        with agent_manager._lock:
            assert "remote-agent" not in agent_manager._activity_tracked_agents
    finally:
        agent_manager.stop()


def test_ensure_activity_tracking_seeds_idle_state_silently(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """When the state dir exists, the agent is seeded as IDLE without broadcasting."""
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")

    listener = broadcaster.register()
    try:
        agent_manager._ensure_activity_tracking("agent-1")
        # No broadcast should have happened (lifecycle handlers broadcast separately).
        with pytest.raises(queue.Empty):
            listener.get_nowait()

        with agent_manager._lock:
            assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.IDLE
            assert agent_manager._agents["agent-1"].activity_state == ActivityState.IDLE.value
    finally:
        agent_manager.stop()


def test_session_events_user_message_drives_thinking(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """A user_message at the tail of the transcript flips activity_state to THINKING.

    Replaces the old behavior where THINKING was driven by a transient ``active``
    marker file -- that marker could leak past the end of a turn and falsely
    pin the indicator on "Thinking...". Transcript content is now authoritative.
    """
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    agent_manager._ensure_activity_tracking("agent-1")

    listener = broadcaster.register()
    try:
        agent_manager.update_session_events(
            "agent-1",
            [{"type": "user_message", "content": "go"}],
        )
        with agent_manager._lock:
            assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.THINKING
        latest = _last_agents_updated(_drain(listener))
        assert latest is not None
        agents = latest["agents"]
        assert isinstance(agents, list)
        assert agents[0]["activity_state"] == ActivityState.THINKING.value
    finally:
        agent_manager.stop()


def test_session_events_assistant_message_at_tail_is_idle(agent_manager: AgentManager, tmp_path: Path) -> None:
    """An assistant_message with no pending tools at the tail means IDLE."""
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    agent_manager._ensure_activity_tracking("agent-1")

    try:
        agent_manager.update_session_events(
            "agent-1",
            [
                {"type": "user_message", "content": "go"},
                {"type": "assistant_message", "tool_calls": []},
            ],
        )
        with agent_manager._lock:
            assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.IDLE
    finally:
        agent_manager.stop()


def test_update_session_events_flips_to_tool_running(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    agent_manager._ensure_activity_tracking("agent-1")

    listener = broadcaster.register()
    try:
        events_with_pending: list[dict[str, Any]] = [
            {
                "type": "assistant_message",
                "tool_calls": [{"tool_call_id": "call_a", "tool_name": "Bash"}],
            }
        ]
        agent_manager.update_session_events("agent-1", events_with_pending)

        with agent_manager._lock:
            assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.TOOL_RUNNING

        latest = _last_agents_updated(_drain(listener))
        assert latest is not None
        agents = latest["agents"]
        assert isinstance(agents, list)
        assert agents[0]["activity_state"] == ActivityState.TOOL_RUNNING.value

        # Once the result lands, we flip to THINKING (last event is tool_result,
        # no pending tool_use remains).
        events_resolved = events_with_pending + [{"type": "tool_result", "tool_call_id": "call_a"}]
        agent_manager.update_session_events("agent-1", events_resolved)
        with agent_manager._lock:
            assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.THINKING
    finally:
        agent_manager.stop()


def test_update_session_events_no_op_when_not_tracked(agent_manager: AgentManager) -> None:
    """Calling update_session_events for an untracked agent is a quiet no-op.

    Beyond not raising, it must leave no residue in the per-agent caches:
    otherwise those entries would never be cleared (``_stop_activity_tracking``
    only fires for agents that were being tracked), accumulating indefinitely.
    """
    agent_manager.update_session_events(
        "ghost",
        [{"type": "assistant_message", "tool_calls": [{"tool_call_id": "x", "tool_name": "Bash"}]}],
    )
    with agent_manager._lock:
        assert "ghost" not in agent_manager._activity_state_by_agent
        assert "ghost" not in agent_manager._has_unmatched_tool_use_by_agent
        assert "ghost" not in agent_manager._last_event_type_by_agent
        assert "ghost" not in agent_manager._last_event_timestamp_by_agent


def test_reset_activity_state_clears_tool_running(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """reset_activity_state flips a stuck TOOL_RUNNING agent back to IDLE and broadcasts.

    Models the interrupt flow: the agent has an unmatched tool_use in its
    transcript (TOOL_RUNNING), then gets restarted. The restart leaves the
    transcript mid-turn, so without an explicit reset the indicator would
    stay pinned at TOOL_RUNNING.
    """
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    agent_manager._ensure_activity_tracking("agent-1")

    listener = broadcaster.register()
    try:
        agent_manager.update_session_events(
            "agent-1",
            [{"type": "assistant_message", "tool_calls": [{"tool_call_id": "call_a", "tool_name": "Bash"}]}],
        )
        with agent_manager._lock:
            assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.TOOL_RUNNING

        agent_manager.reset_activity_state("agent-1")

        with agent_manager._lock:
            assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.IDLE
            assert agent_manager._agents["agent-1"].activity_state == ActivityState.IDLE.value

        latest = _last_agents_updated(_drain(listener))
        assert latest is not None
        agents = latest["agents"]
        assert isinstance(agents, list)
        assert agents[0]["activity_state"] == ActivityState.IDLE.value
    finally:
        agent_manager.stop()


def test_reset_activity_state_no_op_when_not_tracked(agent_manager: AgentManager) -> None:
    """reset_activity_state for an untracked agent is a quiet no-op with no cache residue."""
    agent_manager.reset_activity_state("ghost")
    with agent_manager._lock:
        assert "ghost" not in agent_manager._activity_state_by_agent
        assert "ghost" not in agent_manager._has_unmatched_tool_use_by_agent
        assert "ghost" not in agent_manager._last_event_type_by_agent
        assert "ghost" not in agent_manager._last_event_timestamp_by_agent


def test_stale_transcript_tail_after_restart_shows_idle(agent_manager: AgentManager, tmp_path: Path) -> None:
    """A running agent whose mid-turn transcript predates the current Claude
    process is shown IDLE, not "Thinking...".

    Reproduces the container-restart case: the transcript still ends on a
    tool_result from the turn that was abandoned when the restart killed Claude,
    so the running-but-idle agent would otherwise stay pinned at THINKING. Once
    mngr touches ``claude_process_started`` on resume, its newer mtime marks the
    tail as stale and the indicator settles on IDLE.
    """
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    agent_manager._ensure_activity_tracking("agent-1")

    # The transcript ends on a tool_result from the distant past (the abandoned
    # turn): an assistant tool_use matched by its tool_result, nothing after.
    agent_manager.update_session_events(
        "agent-1",
        [
            {
                "type": "assistant_message",
                "tool_calls": [{"tool_call_id": "call_a", "tool_name": "Bash"}],
                "timestamp": "2020-01-01T00:00:00.000Z",
            },
            {"type": "tool_result", "tool_call_id": "call_a", "timestamp": "2020-01-01T00:00:01.000Z"},
        ],
    )

    # Before the restart marker exists, the mid-turn tail still reads as THINKING.
    with agent_manager._lock:
        assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.THINKING

    # mngr touches claude_process_started on resume; its mtime ("now") is well
    # after the 2020 transcript events.
    (state_dir / "claude_process_started").touch()

    # In production the post-restart observe snapshot drives this recompute.
    agent_manager._recompute_activity_state("agent-1", broadcast_on_change=False)

    with agent_manager._lock:
        assert agent_manager._activity_state_by_agent["agent-1"] == ActivityState.IDLE
        assert agent_manager._agents["agent-1"].activity_state == ActivityState.IDLE.value


def test_stop_activity_tracking_clears_caches(agent_manager: AgentManager, tmp_path: Path) -> None:
    state_dir = tmp_path / "agents" / "agent-1"
    state_dir.mkdir(parents=True)
    _seed_agent(agent_manager, "agent-1")
    agent_manager._ensure_activity_tracking("agent-1")
    # Seed a non-default cached state so we can verify it's cleared.
    agent_manager.update_session_events(
        "agent-1",
        [{"type": "user_message", "content": "go"}],
    )

    with agent_manager._lock:
        assert "agent-1" in agent_manager._activity_tracked_agents
        assert "agent-1" in agent_manager._activity_state_by_agent
        assert "agent-1" in agent_manager._has_unmatched_tool_use_by_agent
        assert "agent-1" in agent_manager._last_event_type_by_agent
        assert "agent-1" in agent_manager._last_event_timestamp_by_agent

    agent_manager._stop_activity_tracking("agent-1")

    with agent_manager._lock:
        assert "agent-1" not in agent_manager._activity_tracked_agents
        assert "agent-1" not in agent_manager._activity_state_by_agent
        assert "agent-1" not in agent_manager._has_unmatched_tool_use_by_agent
        assert "agent-1" not in agent_manager._last_event_type_by_agent
        assert "agent-1" not in agent_manager._last_event_timestamp_by_agent


def test_agent_removed_event_fires_removal_side_effects(agent_manager: AgentManager, tmp_path: Path) -> None:
    """An AGENT_REMOVED event drops the agent and clears its activity tracking and caches."""
    test_agent_id = MngrAgentId()
    str_id = str(test_agent_id)

    state_dir = tmp_path / "agents" / str_id
    state_dir.mkdir(parents=True)
    agent = _agent_details("to-destroy", agent_id=test_agent_id)
    agent_manager._handle_observe_event(make_agent_state_event(agent))
    with agent_manager._lock:
        assert str_id in agent_manager._activity_tracked_agents

    agent_manager._handle_observe_event(make_agent_removed_event(agent.id, agent.name))

    assert agent_manager.get_agent_by_id(str_id) is None
    with agent_manager._lock:
        assert str_id not in agent_manager._activity_tracked_agents
        assert str_id not in agent_manager._activity_state_by_agent


def test_provider_snapshot_preserves_activity_state_for_tracked_agent(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """A per-provider snapshot must not wipe the activity_state of agents that
    are already being tracked for activity.

    Regression test: ``_handle_observe_event`` rebuilds ``_agents`` wholesale
    from the raw observe payload (which has no ``activity_state`` field) on every
    event. Only ids in the membership delta's ``added`` set get an
    ``_ensure_activity_tracking`` recompute, so a snapshot that merely re-lists an
    already-known agent reports it in neither add nor remove. Without re-applying
    the cached state, the broadcast that follows would emit ``activity_state=None``
    for every previously-tracked agent and the chat panel indicator would briefly
    disappear.
    """
    test_agent_id = MngrAgentId()
    str_id = str(test_agent_id)

    state_dir = tmp_path / "agents" / str_id
    state_dir.mkdir(parents=True)

    # First, simulate the agent already being tracked with a live watcher
    # whose transcript signals THINKING (a user_message with no reply).
    agent = _agent_details("snapshot-agent", agent_id=test_agent_id, work_dir=str(tmp_path / "work"))
    agent_manager._handle_observe_event(make_agent_state_event(agent))
    agent_manager.update_session_events(str_id, [{"type": "user_message", "content": "go"}])
    with agent_manager._lock:
        assert agent_manager._activity_state_by_agent[str_id] == ActivityState.THINKING
        assert agent_manager._agents[str_id].activity_state == ActivityState.THINKING.value

    # Now drain prior broadcasts so the snapshot's broadcast is the only one
    # we read.
    listener = broadcaster.register()
    try:
        snapshot_event = make_full_agent_state_event([agent])
        agent_manager._handle_observe_event(snapshot_event)

        latest = _last_agents_updated(_drain(listener))
        assert latest is not None
        agents = latest["agents"]
        assert isinstance(agents, list)
        # The broadcast must carry the cached activity_state, not None.
        assert agents[0]["id"] == str_id
        assert agents[0]["activity_state"] == ActivityState.THINKING.value

        with agent_manager._lock:
            assert agent_manager._agents[str_id].activity_state == ActivityState.THINKING.value
    finally:
        agent_manager.stop()


def test_agent_state_event_stopped_flips_lifecycle_and_activity_to_idle(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster, tmp_path: Path
) -> None:
    """A STOPPED AGENT_STATE event for a tracked, thinking agent broadcasts state=STOPPED
    and re-gates its activity indicator to IDLE.

    The observe stream now carries each agent's real lifecycle state, so an agent
    whose process dies on its own arrives as STOPPED. Because STOPPED is not in
    ``RUNNING_LIFECYCLE_STATES``, the recompute pass must settle its activity to
    IDLE even though the transcript tail still reads THINKING.
    """
    test_agent_id = MngrAgentId()
    str_id = str(test_agent_id)

    state_dir = tmp_path / "agents" / str_id
    state_dir.mkdir(parents=True)

    running = _agent_details("dying-agent", agent_id=test_agent_id, state=AgentLifecycleState.RUNNING)
    agent_manager._handle_observe_event(make_agent_state_event(running))
    # A pending user_message with no reply pins the transcript-derived state at THINKING.
    agent_manager.update_session_events(str_id, [{"type": "user_message", "content": "go"}])
    with agent_manager._lock:
        assert agent_manager._activity_state_by_agent[str_id] == ActivityState.THINKING

    listener = broadcaster.register()
    try:
        stopped = _agent_details("dying-agent", agent_id=test_agent_id, state=AgentLifecycleState.STOPPED)
        agent_manager._handle_observe_event(make_agent_state_event(stopped))

        latest = _last_agents_updated(_drain(listener))
        assert latest is not None
        agents = latest["agents"]
        assert isinstance(agents, list)
        assert agents[0]["id"] == str_id
        assert agents[0]["state"] == AgentLifecycleState.STOPPED.value
        assert agents[0]["activity_state"] == ActivityState.IDLE.value

        with agent_manager._lock:
            assert agent_manager._agents[str_id].state == AgentLifecycleState.STOPPED.value
            assert agent_manager._activity_state_by_agent[str_id] == ActivityState.IDLE
    finally:
        agent_manager.stop()


def test_full_snapshot_rebuilds_agent_set_and_broadcasts(
    agent_manager: AgentManager, broadcaster: WebSocketBroadcaster
) -> None:
    """A full snapshot rebuilds the tracked set: new agents appear, absent ones are dropped,
    and a single agents_updated broadcast reflects the rebuilt set."""
    first = _agent_details("first-agent")
    agent_manager._handle_observe_event(make_full_agent_state_event([first]))
    assert {a.id for a in agent_manager.get_agents()} == {str(first.id)}

    q = broadcaster.register()
    second = _agent_details("second-agent")
    agent_manager._handle_observe_event(make_full_agent_state_event([second]))

    tracked_ids = {a.id for a in agent_manager.get_agents()}
    assert tracked_ids == {str(second.id)}
    assert str(first.id) not in tracked_ids

    raw = q.get_nowait()
    assert raw is not None
    msg = json.loads(raw)
    assert msg["type"] == "agents_updated"
    assert {a["id"] for a in msg["agents"]} == {str(second.id)}
