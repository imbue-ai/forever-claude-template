"""Tests for agent_discovery module."""

from pathlib import Path

import pytest

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
