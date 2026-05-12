"""Tests for agent_discovery module."""

from pathlib import Path

import pytest

from imbue.minds_workspace_server.agent_discovery import read_claude_config_dir_from_env_file
from imbue.minds_workspace_server.agent_discovery import read_tickets_dir_from_env_file


def test_reads_claude_config_dir_from_env_file(tmp_path: Path) -> None:
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    env_file = agent_state_dir / "env"
    env_file.write_text('CLAUDE_CONFIG_DIR="/custom/config/dir"\n')

    result = read_claude_config_dir_from_env_file(agent_state_dir)

    assert result == Path("/custom/config/dir")


def test_falls_back_to_conventional_path_when_env_file_missing(tmp_path: Path) -> None:
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    conventional = agent_state_dir / "plugin" / "claude" / "anthropic"
    conventional.mkdir(parents=True)

    result = read_claude_config_dir_from_env_file(agent_state_dir)

    assert result == conventional


def test_falls_back_to_conventional_path_when_env_has_no_config_dir(tmp_path: Path) -> None:
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    env_file = agent_state_dir / "env"
    env_file.write_text("OTHER_VAR=something\n")
    conventional = agent_state_dir / "plugin" / "claude" / "anthropic"
    conventional.mkdir(parents=True)

    result = read_claude_config_dir_from_env_file(agent_state_dir)

    assert result == conventional


def test_falls_back_to_home_claude_when_nothing_else_exists(tmp_path: Path) -> None:
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()

    result = read_claude_config_dir_from_env_file(agent_state_dir)

    assert result == Path.home() / ".claude"


def test_tickets_dir_read_from_agent_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    (agent_state_dir / "env").write_text("TICKETS_DIR=/from/env/file\n")
    monkeypatch.setenv("TICKETS_DIR", "/from/process/env")

    result = read_tickets_dir_from_env_file(agent_state_dir, tmp_path / "work")

    assert result == Path("/from/env/file")


def test_tickets_dir_falls_back_to_process_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    (agent_state_dir / "env").write_text("OTHER=x\n")
    monkeypatch.setenv("TICKETS_DIR", "/from/process/env")

    result = read_tickets_dir_from_env_file(agent_state_dir, tmp_path / "work")

    assert result == Path("/from/process/env")


def test_tickets_dir_falls_back_to_work_dir_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    monkeypatch.delenv("TICKETS_DIR", raising=False)
    work_dir = tmp_path / "work"

    result = read_tickets_dir_from_env_file(agent_state_dir, work_dir)

    assert result == work_dir / ".tickets"
