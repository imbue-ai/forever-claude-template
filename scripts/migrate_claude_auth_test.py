"""Tests for the host-env -> settings.json Claude auth migration script.

The script's file-move phase is exercised directly (via importlib, the same
pattern as claude_oom_launch_test.py); the detached restart phase is covered
by the restart tests in apps/system_interface/claude_auth_test.py, which
test the same `ClaudeAuthService.restart_all_claude_agents` the script calls.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent / "migrate_claude_auth.py"
_spec = importlib.util.spec_from_file_location("migrate_claude_auth", _SCRIPT)
assert _spec is not None and _spec.loader is not None
migration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migration)


@pytest.fixture
def workspace_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    config_dir = tmp_path / "claude-config"
    config_dir.mkdir()
    monkeypatch.setenv("MNGR_HOST_DIR", str(host_dir))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    return host_dir, config_dir


def test_migrate_moves_keys_into_settings_and_scrubs_host_env(workspace_dirs: tuple[Path, Path]) -> None:
    host_dir, config_dir = workspace_dirs
    (host_dir / "env").write_text(
        "CLAUDE_CONFIG_DIR=/mngr/claude\nANTHROPIC_API_KEY=sk-old-key\nANTHROPIC_BASE_URL=https://litellm.example\n"
    )

    changed = migration._migrate_env_files()

    assert changed is True
    settings = json.loads((config_dir / "settings.json").read_text())
    assert settings["env"] == {
        "ANTHROPIC_API_KEY": "sk-old-key",
        "ANTHROPIC_BASE_URL": "https://litellm.example",
    }
    host_env_text = (host_dir / "env").read_text()
    assert "ANTHROPIC_API_KEY" not in host_env_text
    assert "ANTHROPIC_BASE_URL" not in host_env_text
    # Non-managed host env keys survive the scrub.
    assert "CLAUDE_CONFIG_DIR=/mngr/claude" in host_env_text


def test_migrate_is_noop_when_host_env_holds_no_auth_keys(workspace_dirs: tuple[Path, Path]) -> None:
    host_dir, config_dir = workspace_dirs
    (host_dir / "env").write_text("CLAUDE_CONFIG_DIR=/mngr/claude\n")

    changed = migration._migrate_env_files()

    assert changed is False
    assert not (config_dir / "settings.json").exists()


def test_migrate_rerun_after_success_is_noop(workspace_dirs: tuple[Path, Path]) -> None:
    host_dir, _config_dir = workspace_dirs
    (host_dir / "env").write_text("ANTHROPIC_API_KEY=sk-old-key\n")

    assert migration._migrate_env_files() is True
    assert migration._migrate_env_files() is False


def test_migrate_keeps_existing_settings_credentials_over_host_env(workspace_dirs: tuple[Path, Path]) -> None:
    """A modal-written credential outranks the stale host-env one.

    The stale host key is still scrubbed (that is the point of the
    migration), but the settings env block keeps what the modal wrote.
    """
    host_dir, config_dir = workspace_dirs
    (host_dir / "env").write_text("ANTHROPIC_API_KEY=sk-stale\n")
    (config_dir / "settings.json").write_text(json.dumps({"env": {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-modal"}}))

    changed = migration._migrate_env_files()

    assert changed is True
    settings = json.loads((config_dir / "settings.json").read_text())
    assert settings["env"] == {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-modal"}
    assert "sk-stale" not in (host_dir / "env").read_text()
