"""Unit tests for the bootstrap service manager's reconciliation logic."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bootstrap.manager import (
    _build_create_chat_command,
    _compute_actions,
    _ensure_host_claude_config_dir,
    _format_env_file,
    _initialize_workspace_main_branch,
    _maybe_create_initial_chat,
    _parse_env_file,
    _read_host_name,
    _read_main_agent_labels,
    _resolve_services_claude_config_dir,
)


def test_compute_actions_no_changes_when_in_sync() -> None:
    desired = {"a": {"command": "cmd-a", "restart": "never"}}
    current = {"a": {"window_name": "svc-a", "command": "cmd-a"}}
    stops, starts = _compute_actions(desired, current)
    assert stops == []
    assert starts == []


def test_compute_actions_starts_missing_service() -> None:
    desired = {"a": {"command": "cmd-a", "restart": "never"}}
    current: dict[str, dict[str, str]] = {}
    stops, starts = _compute_actions(desired, current)
    assert stops == []
    assert starts == [("a", "cmd-a")]


def test_compute_actions_stops_removed_service() -> None:
    desired: dict[str, dict] = {}
    current = {"a": {"window_name": "svc-a", "command": "cmd-a"}}
    stops, starts = _compute_actions(desired, current)
    assert stops == ["a"]
    assert starts == []


def test_compute_actions_restarts_on_command_change() -> None:
    desired = {"a": {"command": "cmd-a-new", "restart": "never"}}
    current = {"a": {"window_name": "svc-a", "command": "cmd-a-old"}}
    stops, starts = _compute_actions(desired, current)
    assert stops == ["a"]
    assert starts == [("a", "cmd-a-new")]


def test_compute_actions_treats_unknown_recorded_command_as_change() -> None:
    # A window created by an older manager has no recorded command; reading the
    # user-option yields "". That mismatch should trigger a restart so the new
    # manager takes ownership of the window with a known command.
    desired = {"a": {"command": "cmd-a", "restart": "never"}}
    current = {"a": {"window_name": "svc-a", "command": ""}}
    stops, starts = _compute_actions(desired, current)
    assert stops == ["a"]
    assert starts == [("a", "cmd-a")]


def test_compute_actions_handles_mixed_add_remove_change() -> None:
    desired = {
        "keep": {"command": "k", "restart": "never"},
        "change": {"command": "new", "restart": "never"},
        "add": {"command": "added", "restart": "never"},
    }
    current = {
        "keep": {"window_name": "svc-keep", "command": "k"},
        "change": {"window_name": "svc-change", "command": "old"},
        "remove": {"window_name": "svc-remove", "command": "r"},
    }
    stops, starts = _compute_actions(desired, current)
    assert sorted(stops) == ["change", "remove"]
    assert sorted(starts) == [("add", "added"), ("change", "new")]


# --- Env-file helpers ---


def test_parse_env_file_handles_plain_and_quoted() -> None:
    content = 'A=1\nB="two words"\nC="he said \\"hi\\""\n\n# comment\n'
    parsed = _parse_env_file(content)
    assert parsed == {"A": "1", "B": "two words", "C": 'he said "hi"'}


def test_format_env_file_round_trips_through_parse() -> None:
    env = {"FOO": "bar", "PATH_WITH_SPACE": "/a b/c"}
    parsed = _parse_env_file(_format_env_file(env))
    assert parsed == env


# --- _resolve_services_claude_config_dir ---


def test_resolve_services_claude_config_dir_returns_per_agent_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))
    resolved = _resolve_services_claude_config_dir()
    assert resolved == tmp_path / "plugin" / "claude" / "anthropic"


def test_resolve_services_claude_config_dir_returns_none_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MNGR_AGENT_STATE_DIR", raising=False)
    assert _resolve_services_claude_config_dir() is None


# --- _ensure_host_claude_config_dir ---


def test_ensure_host_claude_config_dir_writes_when_env_file_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    target = Path("/some/per-agent/path")
    _ensure_host_claude_config_dir(target)
    parsed = _parse_env_file((tmp_path / "env").read_text())
    assert parsed == {"CLAUDE_CONFIG_DIR": str(target)}


def test_ensure_host_claude_config_dir_preserves_other_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    (tmp_path / "env").write_text(_format_env_file({"OTHER": "preexisting"}))
    target = Path("/some/per-agent/path")
    _ensure_host_claude_config_dir(target)
    parsed = _parse_env_file((tmp_path / "env").read_text())
    assert parsed == {"OTHER": "preexisting", "CLAUDE_CONFIG_DIR": str(target)}


def test_ensure_host_claude_config_dir_no_rewrite_when_value_matches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    target = Path("/some/per-agent/path")
    env_file = tmp_path / "env"
    env_file.write_text(_format_env_file({"CLAUDE_CONFIG_DIR": str(target)}))
    mtime_before = env_file.stat().st_mtime_ns
    _ensure_host_claude_config_dir(target)
    assert env_file.stat().st_mtime_ns == mtime_before


def test_ensure_host_claude_config_dir_overwrites_drifted_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    env_file = tmp_path / "env"
    env_file.write_text(_format_env_file({"CLAUDE_CONFIG_DIR": "/stale/path"}))
    target = Path("/new/path")
    _ensure_host_claude_config_dir(target)
    parsed = _parse_env_file(env_file.read_text())
    assert parsed["CLAUDE_CONFIG_DIR"] == str(target)


def test_ensure_host_claude_config_dir_skips_when_host_dir_env_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("MNGR_HOST_DIR", raising=False)
    # Should silently no-op rather than raise.
    _ensure_host_claude_config_dir(tmp_path / "ignored")


# --- _read_host_name ---


def test_read_host_name_returns_value_from_data_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    (tmp_path / "data.json").write_text(json.dumps({"host_name": "my-workspace"}))
    assert _read_host_name() == "my-workspace"


def test_read_host_name_returns_none_when_data_json_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    assert _read_host_name() is None


def test_read_host_name_returns_none_when_host_dir_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MNGR_HOST_DIR", raising=False)
    assert _read_host_name() is None


def test_read_host_name_returns_none_when_field_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    (tmp_path / "data.json").write_text(json.dumps({"other": "value"}))
    assert _read_host_name() is None


# --- _read_main_agent_labels ---


def test_read_main_agent_labels_returns_label_dict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-1")
    agent_dir = tmp_path / "agents" / "agent-1"
    agent_dir.mkdir(parents=True)
    (agent_dir / "data.json").write_text(
        json.dumps({"labels": {"workspace": "my-ws", "is_primary": "true"}})
    )
    assert _read_main_agent_labels() == {"workspace": "my-ws", "is_primary": "true"}


def test_read_main_agent_labels_returns_empty_when_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MNGR_HOST_DIR", raising=False)
    monkeypatch.delenv("MNGR_AGENT_ID", raising=False)
    assert _read_main_agent_labels() == {}


def test_read_main_agent_labels_returns_empty_when_data_json_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-1")
    assert _read_main_agent_labels() == {}


def test_read_main_agent_labels_returns_empty_when_labels_field_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-1")
    agent_dir = tmp_path / "agents" / "agent-1"
    agent_dir.mkdir(parents=True)
    (agent_dir / "data.json").write_text(json.dumps({"other": "value"}))
    assert _read_main_agent_labels() == {}


# --- _build_create_chat_command ---


def test_build_create_chat_command_includes_welcome_and_template() -> None:
    cmd = _build_create_chat_command("my-workspace", {"workspace": "my-workspace"})
    assert cmd[:3] == ["mngr", "create", "my-workspace"]
    assert "--template" in cmd
    assert cmd[cmd.index("--template") + 1] == "chat"
    assert "--message" in cmd
    assert cmd[cmd.index("--message") + 1] == "/welcome"
    assert "--no-connect" in cmd


def test_build_create_chat_command_includes_transfer_none() -> None:
    """`--transfer none` makes mngr skip the per-agent worktree, so the chat
    agent reuses the services agent's work_dir. Without it, mngr collides
    with the services agent's existing `mngr/<host>` branch."""
    cmd = _build_create_chat_command("my-workspace", {"workspace": "my-workspace"})
    assert "--transfer" in cmd
    assert cmd[cmd.index("--transfer") + 1] == "none"


def test_build_create_chat_command_passes_workspace_label() -> None:
    cmd = _build_create_chat_command("my-workspace", {"workspace": "my-workspace"})
    # The workspace label should be present exactly once.
    labels = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--label"]
    assert "workspace=my-workspace" in labels


def test_build_create_chat_command_passes_project_label_when_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cmd = _build_create_chat_command("ws", {"workspace": "ws", "project": "my-project"})
    labels = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--label"]
    assert "project=my-project" in labels


def test_build_create_chat_command_omits_project_label_when_missing() -> None:
    cmd = _build_create_chat_command("ws", {"workspace": "ws"})
    labels = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--label"]
    assert all(not label.startswith("project=") for label in labels)


# --- _maybe_create_initial_chat ---


class _StubSubprocess:
    """Capture-and-replay double for subprocess.run used by the chat-create call."""

    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def run(
        self,
        cmd: list[str],
        capture_output: bool = False,
        text: bool = False,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del capture_output, text, check  # keyword-only signature mirrors stdlib.
        self.calls.append(cmd)
        return subprocess.CompletedProcess(
            args=cmd, returncode=self.returncode, stdout="", stderr=""
        )


@pytest.fixture
def _bootstrap_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Common setup: MNGR_HOST_DIR rooted in tmp_path, a workspace in data.json,
    a chdir into tmp_path so the signal file lands somewhere ephemeral.

    Explicitly unsets MNGR_AGENT_WORK_DIR so `_initialize_workspace_main_branch`
    short-circuits in tests that don't care about the git initialization path;
    tests that DO want that path can monkeypatch MNGR_AGENT_WORK_DIR back in.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-1")
    monkeypatch.delenv("MNGR_AGENT_WORK_DIR", raising=False)
    (tmp_path / "data.json").write_text(json.dumps({"host_name": "my-workspace"}))
    agent_dir = tmp_path / "agents" / "agent-1"
    agent_dir.mkdir(parents=True)
    (agent_dir / "data.json").write_text(
        json.dumps({"labels": {"workspace": "my-workspace", "is_primary": "true"}})
    )
    return tmp_path


def test_maybe_create_initial_chat_creates_and_writes_signal(
    monkeypatch: pytest.MonkeyPatch, _bootstrap_env: Path
) -> None:
    stub = _StubSubprocess(returncode=0)
    monkeypatch.setattr("bootstrap.manager.subprocess.run", stub.run)
    _maybe_create_initial_chat()
    assert len(stub.calls) == 1
    assert (_bootstrap_env / "runtime" / "initial_chat_created").exists()


def test_maybe_create_initial_chat_skips_when_signal_present(
    monkeypatch: pytest.MonkeyPatch, _bootstrap_env: Path
) -> None:
    runtime = _bootstrap_env / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "initial_chat_created").write_text("")
    stub = _StubSubprocess(returncode=0)
    monkeypatch.setattr("bootstrap.manager.subprocess.run", stub.run)
    _maybe_create_initial_chat()
    assert stub.calls == []


def test_maybe_create_initial_chat_skips_signal_on_failure(
    monkeypatch: pytest.MonkeyPatch, _bootstrap_env: Path
) -> None:
    stub = _StubSubprocess(returncode=1)
    monkeypatch.setattr("bootstrap.manager.subprocess.run", stub.run)
    _maybe_create_initial_chat()
    assert len(stub.calls) == 1
    assert not (_bootstrap_env / "runtime" / "initial_chat_created").exists()


def test_maybe_create_initial_chat_skips_when_host_name_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-1")
    monkeypatch.delenv("MNGR_AGENT_WORK_DIR", raising=False)
    # No data.json at all -> host_name resolution fails.
    stub = _StubSubprocess(returncode=0)
    monkeypatch.setattr("bootstrap.manager.subprocess.run", stub.run)
    _maybe_create_initial_chat()
    assert stub.calls == []
    assert not (tmp_path / "runtime" / "initial_chat_created").exists()


# --- _initialize_workspace_main_branch ---


def _git_in(work_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Helper for tests: run a real git command inside `work_dir`."""
    return subprocess.run(
        ["git", *args], cwd=work_dir, capture_output=True, text=True, check=False
    )


def test_initialize_workspace_main_branch_commits_and_renames(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end: a real git repo on `mngr/foo` with uncommitted changes ends
    up on `main` with the working tree committed."""
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    _git_in(work_dir, "init", "--initial-branch=main", "-q")
    _git_in(work_dir, "config", "user.email", "seed@test.local")
    _git_in(work_dir, "config", "user.name", "seed")
    (work_dir / "README.md").write_text("seed\n")
    _git_in(work_dir, "add", "-A")
    _git_in(work_dir, "commit", "-qm", "seed")
    # Branch the way agent_creator.py:447 does: `:mngr/<host_name>` makes a
    # new branch off current. Then add some uncommitted content (simulating
    # the desktop client's _rsync_worktree_over_clone).
    _git_in(work_dir, "checkout", "-q", "-b", "mngr/foo")
    (work_dir / "rsynced.txt").write_text("uncommitted from rsync\n")

    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(work_dir))
    _initialize_workspace_main_branch()

    branch = _git_in(work_dir, "branch", "--show-current").stdout.strip()
    status = _git_in(work_dir, "status", "--porcelain").stdout.strip()
    head_msg = _git_in(work_dir, "log", "-1", "--format=%s").stdout.strip()
    assert branch == "main"
    assert status == ""  # all the uncommitted rsync content was captured
    assert head_msg == "Initial workspace commit"


def test_initialize_workspace_main_branch_skips_when_work_dir_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If MNGR_AGENT_WORK_DIR isn't set, no git invocations happen."""
    monkeypatch.delenv("MNGR_AGENT_WORK_DIR", raising=False)
    stub = _StubSubprocess(returncode=0)
    monkeypatch.setattr("bootstrap.manager.subprocess.run", stub.run)
    _initialize_workspace_main_branch()
    assert stub.calls == []


def test_initialize_workspace_main_branch_is_idempotent_on_clean_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second invocation on an already-clean `main` branch is a no-op for
    the user (we make an empty allow-empty commit, but it's harmless)."""
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    _git_in(work_dir, "init", "--initial-branch=main", "-q")
    _git_in(work_dir, "config", "user.email", "seed@test.local")
    _git_in(work_dir, "config", "user.name", "seed")
    (work_dir / "README.md").write_text("seed\n")
    _git_in(work_dir, "add", "-A")
    _git_in(work_dir, "commit", "-qm", "seed")
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(work_dir))
    _initialize_workspace_main_branch()
    branch = _git_in(work_dir, "branch", "--show-current").stdout.strip()
    assert branch == "main"
