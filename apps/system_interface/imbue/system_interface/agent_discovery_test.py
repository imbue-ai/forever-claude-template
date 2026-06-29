"""Tests for agent_discovery module."""

from collections.abc import Iterator
from collections.abc import Sequence
from pathlib import Path

import pytest

from imbue.mngr.api.find import AgentMatch
from imbue.mngr.api.message import MessageResult
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.system_interface.agent_discovery import _get_mngr_context
from imbue.system_interface.agent_discovery import _send_message_to_agent
from imbue.system_interface.agent_discovery import read_claude_config_dir_from_env_file


def test_reads_claude_config_dir_from_env_file(tmp_path: Path) -> None:
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    env_file = agent_state_dir / "env"
    env_file.write_text('CLAUDE_CONFIG_DIR="/custom/config/dir"\n')

    result = read_claude_config_dir_from_env_file(agent_state_dir)

    assert result == Path("/custom/config/dir")


def test_falls_back_to_host_env_when_per_agent_env_lacks_config_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Mimics use_env_config_dir=True chat agents: mngr_claude does not write
    CLAUDE_CONFIG_DIR to the per-agent env file, but the bootstrap wrote it
    to $MNGR_HOST_DIR/env. Without this layer the system_interface's
    session_watcher pointed at ~/.claude and chat messages never showed up
    in the UI."""
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    (host_dir / "env").write_text("MNGR_HOST_DIR=/mngr\nCLAUDE_CONFIG_DIR=/shared/claude/config\n")
    agent_state_dir = host_dir / "agents" / "agent-1"
    agent_state_dir.mkdir(parents=True)
    # Per-agent env exists but doesn't carry CLAUDE_CONFIG_DIR.
    (agent_state_dir / "env").write_text("MNGR_AGENT_ID=agent-1\nLATCHKEY_GATEWAY=...\n")

    monkeypatch.setenv("MNGR_HOST_DIR", str(host_dir))
    result = read_claude_config_dir_from_env_file(agent_state_dir)

    assert result == Path("/shared/claude/config")


def test_per_agent_env_takes_precedence_over_host_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When both are set, the per-agent value wins -- matches the runtime
    chain where the agent's env file is sourced AFTER the host env file."""
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    (host_dir / "env").write_text("CLAUDE_CONFIG_DIR=/host/value\n")
    agent_state_dir = host_dir / "agents" / "agent-1"
    agent_state_dir.mkdir(parents=True)
    (agent_state_dir / "env").write_text("CLAUDE_CONFIG_DIR=/per-agent/value\n")

    monkeypatch.setenv("MNGR_HOST_DIR", str(host_dir))
    result = read_claude_config_dir_from_env_file(agent_state_dir)

    assert result == Path("/per-agent/value")


def test_falls_back_to_conventional_path_when_env_file_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("MNGR_HOST_DIR", raising=False)
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    conventional = agent_state_dir / "plugin" / "claude" / "anthropic"
    conventional.mkdir(parents=True)

    result = read_claude_config_dir_from_env_file(agent_state_dir)

    assert result == conventional


def test_falls_back_to_conventional_path_when_env_has_no_config_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("MNGR_HOST_DIR", raising=False)
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    env_file = agent_state_dir / "env"
    env_file.write_text("OTHER_VAR=something\n")
    conventional = agent_state_dir / "plugin" / "claude" / "anthropic"
    conventional.mkdir(parents=True)

    result = read_claude_config_dir_from_env_file(agent_state_dir)

    assert result == conventional


def test_falls_back_to_home_claude_when_nothing_else_exists(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("MNGR_HOST_DIR", raising=False)
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()

    result = read_claude_config_dir_from_env_file(agent_state_dir)

    assert result == Path.home() / ".claude"


@pytest.fixture
def mngr_ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[MngrContext]:
    """A real MngrContext rooted at empty tmp dirs (no project config files to load)."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    monkeypatch.setenv("MNGR_PROJECT_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path / "host"))
    ctx, cg = _get_mngr_context()
    try:
        yield ctx
    finally:
        cg.__exit__(None, None, None)


_AGENT_ID = AgentId("agent-00000000000000000000000000000001")


def _make_match(agent_id: AgentId = _AGENT_ID, host: str = "host-a") -> AgentMatch:
    return AgentMatch(
        agent_id=agent_id,
        agent_name=AgentName("alpha"),
        host_id=HostId.generate(),
        host_name=HostName(host),
        provider_name=ProviderInstanceName("local"),
    )


def test_known_location_is_messaged_without_discovery(mngr_ctx: MngrContext) -> None:
    match = _make_match()
    discover_calls: list[AgentId] = []
    send_calls: list[tuple[AgentMatch, ...]] = []

    def _discover(agent_id: AgentId, ctx: MngrContext) -> Sequence[AgentMatch]:
        discover_calls.append(agent_id)
        return ()

    def _send(matches: Sequence[AgentMatch], message: str, ctx: MngrContext) -> MessageResult:
        send_calls.append(tuple(matches))
        return MessageResult(successful_agents=[str(m.agent_id) for m in matches])

    assert _send_message_to_agent(_AGENT_ID, "hi", mngr_ctx, (match,), discover=_discover, send=_send)
    assert discover_calls == []
    assert send_calls == [(match,)]


def test_empty_known_locations_falls_back_to_discovery(mngr_ctx: MngrContext) -> None:
    discovered = _make_match()
    discover_calls: list[AgentId] = []
    send_calls: list[tuple[AgentMatch, ...]] = []

    def _discover(agent_id: AgentId, ctx: MngrContext) -> Sequence[AgentMatch]:
        discover_calls.append(agent_id)
        return (discovered,)

    def _send(matches: Sequence[AgentMatch], message: str, ctx: MngrContext) -> MessageResult:
        send_calls.append(tuple(matches))
        return MessageResult(successful_agents=[str(m.agent_id) for m in matches])

    assert _send_message_to_agent(_AGENT_ID, "hi", mngr_ctx, (), discover=_discover, send=_send)
    assert discover_calls == [_AGENT_ID]
    assert send_calls == [(discovered,)]


def test_stale_known_location_falls_back_to_discovery(mngr_ctx: MngrContext) -> None:
    stale = _make_match(host="host-a")
    fresh = _make_match(host="host-b")
    discover_calls: list[AgentId] = []
    send_calls: list[tuple[AgentMatch, ...]] = []

    def _discover(agent_id: AgentId, ctx: MngrContext) -> Sequence[AgentMatch]:
        discover_calls.append(agent_id)
        return (fresh,)

    def _send(matches: Sequence[AgentMatch], message: str, ctx: MngrContext) -> MessageResult:
        send_calls.append(tuple(matches))
        # The stale location reaches no agent; the freshly discovered one does.
        reached = [str(m.agent_id) for m in matches if str(m.host_name) == "host-b"]
        return MessageResult(successful_agents=reached)

    assert _send_message_to_agent(_AGENT_ID, "hi", mngr_ctx, (stale,), discover=_discover, send=_send)
    assert discover_calls == [_AGENT_ID]
    assert send_calls == [(stale,), (fresh,)]


def test_returns_false_when_nothing_reachable(mngr_ctx: MngrContext) -> None:
    def _discover(agent_id: AgentId, ctx: MngrContext) -> Sequence[AgentMatch]:
        return ()

    def _send(matches: Sequence[AgentMatch], message: str, ctx: MngrContext) -> MessageResult:
        return MessageResult(successful_agents=[])

    assert _send_message_to_agent(_AGENT_ID, "hi", mngr_ctx, (), discover=_discover, send=_send) is False
