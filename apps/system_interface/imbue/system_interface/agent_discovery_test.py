"""Tests for agent_discovery module."""

from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.api.find import AgentMatch
from imbue.mngr.api.message import MessageResult
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.system_interface.agent_discovery import _AgentMatchCache
from imbue.system_interface.agent_discovery import _send_to_cached_agent
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


def _make_match(name: str, host: str = "host-a") -> AgentMatch:
    return AgentMatch(
        agent_id=AgentId.generate(),
        agent_name=AgentName(name),
        host_id=HostId.generate(),
        host_name=HostName(host),
        provider_name=ProviderInstanceName("local"),
    )


class _RecordingResolver(MutableModel):
    """Stand-in for find_all_agents: returns a fixed result and records calls."""

    result: tuple[AgentMatch, ...]
    calls: list[str] = Field(default_factory=list)

    def __call__(self, agent_name: str) -> Sequence[AgentMatch]:
        self.calls.append(agent_name)
        return self.result


class _RecordingSender(MutableModel):
    """Stand-in for send_message_to_agents: reaches only the live agents given."""

    live_agent_ids: frozenset[AgentId]
    calls: list[tuple[AgentMatch, ...]] = Field(default_factory=list)

    def __call__(self, matches: Sequence[AgentMatch]) -> MessageResult:
        self.calls.append(tuple(matches))
        reached = [str(m.agent_name) for m in matches if m.agent_id in self.live_agent_ids]
        return MessageResult(successful_agents=reached)


def test_cache_miss_resolves_caches_and_sends() -> None:
    cache = _AgentMatchCache()
    match = _make_match("alpha")
    resolver = _RecordingResolver(result=(match,))
    sender = _RecordingSender(live_agent_ids=frozenset({match.agent_id}))

    assert _send_to_cached_agent("alpha", cache, resolver, sender) is True
    assert resolver.calls == ["alpha"]
    assert sender.calls == [(match,)]
    assert cache.get("alpha") == match


def test_cache_hit_skips_resolution() -> None:
    cache = _AgentMatchCache()
    match = _make_match("alpha")
    cache.put("alpha", match)
    resolver = _RecordingResolver(result=(_make_match("alpha"),))
    sender = _RecordingSender(live_agent_ids=frozenset({match.agent_id}))

    assert _send_to_cached_agent("alpha", cache, resolver, sender) is True
    assert resolver.calls == []
    assert sender.calls == [(match,)]


def test_stale_cache_hit_reresolves_and_updates() -> None:
    cache = _AgentMatchCache()
    stale = _make_match("alpha")
    fresh = _make_match("alpha")
    cache.put("alpha", stale)
    resolver = _RecordingResolver(result=(fresh,))
    sender = _RecordingSender(live_agent_ids=frozenset({fresh.agent_id}))

    assert _send_to_cached_agent("alpha", cache, resolver, sender) is True
    assert resolver.calls == ["alpha"]
    assert sender.calls == [(stale,), (fresh,)]
    assert cache.get("alpha") == fresh


def test_multiple_matches_are_not_cached() -> None:
    cache = _AgentMatchCache()
    first = _make_match("alpha", host="host-a")
    second = _make_match("alpha", host="host-b")
    resolver = _RecordingResolver(result=(first, second))
    sender = _RecordingSender(live_agent_ids=frozenset({first.agent_id, second.agent_id}))

    assert _send_to_cached_agent("alpha", cache, resolver, sender) is True
    assert sender.calls == [(first, second)]
    assert cache.get("alpha") is None


def test_no_match_returns_false_and_caches_nothing() -> None:
    cache = _AgentMatchCache()
    resolver = _RecordingResolver(result=())
    sender = _RecordingSender(live_agent_ids=frozenset())

    assert _send_to_cached_agent("ghost", cache, resolver, sender) is False
    assert resolver.calls == ["ghost"]
    assert cache.get("ghost") is None


def test_agent_match_cache_put_get_invalidate() -> None:
    cache = _AgentMatchCache()
    match = _make_match("alpha")

    assert cache.get("alpha") is None
    cache.put("alpha", match)
    assert cache.get("alpha") == match
    cache.invalidate("alpha")
    assert cache.get("alpha") is None
    cache.invalidate("alpha")
