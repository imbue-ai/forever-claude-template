"""Unit tests for the bootstrap first-boot setup helpers."""

from __future__ import annotations

import io
import json
import os
import shlex
import subprocess
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from imbue.mngr.cli.output_helpers import write_json_line
from mngr_cli_contract.contract import assert_mngr_argv_valid

from bootstrap.manager import (
    INITIAL_CHAT_AGENT_ID_FILENAME,
    _apply_container_timezone,
    _build_create_chat_command,
    _configure_git_global,
    _create_orphan_runtime_worktree,
    _ensure_host_claude_config_dir,
    _fallback_timezone_for_unknown,
    _fetch_user_timezone,
    _format_env_file,
    _initialize_workspace_main_branch,
    _maybe_create_initial_chat,
    _parse_created_agent_id,
    _parse_env_file,
    _persist_initial_chat_agent_id,
    _read_host_name,
    _read_main_agent_labels,
    _resolve_services_claude_config_dir,
    _seed_caretaker_stamp,
    _write_agent_env_snapshot,
)

# --- _configure_git_global ---


def test_configure_git_global_sets_insteadof_and_hookspath(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Isolate the global git config to a tmp file so the test does not touch the
    # developer's real ~/.gitconfig. _configure_git_global should set both
    # insteadOf rewrites (git@ and ssh://) plus core.hooksPath.
    gitconfig = tmp_path / ".gitconfig"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))

    _configure_git_global()

    insteadof = subprocess.run(
        ["git", "config", "--global", "--get-all", "url.https://github.com/.insteadOf"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    assert "git@github.com:" in insteadof
    assert "ssh://git@github.com/" in insteadof

    hooks_path = subprocess.run(
        ["git", "config", "--global", "core.hooksPath"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    assert hooks_path == "/mngr/code/scripts/git_hooks"


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


def test_build_create_chat_command_carries_no_workspace_label() -> None:
    cmd = _build_create_chat_command("my-workspace", {"workspace": "my-workspace"})
    # The chat agent belongs to its workspace by sharing the host; it carries no
    # workspace label (the label was removed from the naming model).
    labels = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--label"]
    assert all(not label.startswith("workspace=") for label in labels)


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


def test_build_create_chat_command_argv_accepted_by_live_cli() -> None:
    """Confront the emitted argv with the live ``imbue.mngr.main.cli`` tree, so
    a vendor/mngr rename of ``create``/its flags fails here at merge time rather
    than only at host boot. A ``workspace`` label is supplied so the builder's
    label resolution short-circuits without reading host files."""
    argv = _build_create_chat_command("host-1", {"workspace": "ws", "project": "proj"})
    assert_mngr_argv_valid(argv)


def test_build_create_chat_command_requests_json_output() -> None:
    """`--format json` lets the create step read back the new agent's id."""
    cmd = _build_create_chat_command("ws", {"workspace": "ws"})
    assert "--format" in cmd
    assert cmd[cmd.index("--format") + 1] == "json"


# --- _parse_created_agent_id ---


def test_parse_created_agent_id_reads_agent_id_from_json_object() -> None:
    stdout = '{"agent_id": "agent-abc", "host_id": "host-1", "host_name": "ws"}\n'
    assert _parse_created_agent_id(stdout) == "agent-abc"


def test_parse_created_agent_id_returns_none_when_absent() -> None:
    assert _parse_created_agent_id('{"host_id": "host-1"}') is None
    assert _parse_created_agent_id("not json at all") is None
    assert _parse_created_agent_id("") is None


def test_parse_created_agent_id_reads_live_mngr_json_output() -> None:
    """Confront the parser with mngr's real `--format json` serializer, so a
    vendor/mngr switch to pretty-printed or JSONL create output fails here at
    merge time rather than only at host boot. `write_json_line` is exactly what
    `mngr create`'s JSON branch calls (one compact object on stdout)."""
    result_data = {
        "agent_id": "agent-0123456789abcdef0123456789abcdef",
        "host_id": "host-1",
        "host_name": "my-workspace",
    }
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        write_json_line(result_data)
    assert _parse_created_agent_id(buffer.getvalue()) == result_data["agent_id"]


# --- _persist_initial_chat_agent_id ---


def test_persist_initial_chat_agent_id_writes_sidecar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    _persist_initial_chat_agent_id("agent-abc")
    assert (tmp_path / INITIAL_CHAT_AGENT_ID_FILENAME).read_text() == "agent-abc"


def test_persist_initial_chat_agent_id_skips_when_host_dir_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("MNGR_HOST_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    _persist_initial_chat_agent_id("agent-abc")
    assert not (tmp_path / INITIAL_CHAT_AGENT_ID_FILENAME).exists()


# --- _maybe_create_initial_chat ---


class _StubSubprocess:
    """Capture-and-replay double for subprocess.run used by the chat-create flow.

    `_maybe_create_initial_chat` now runs two mngr commands: `mngr list`
    (the adopt-existing lookup) and `mngr create`. Replies dispatch on the
    subcommand (cmd[1]); the default reply covers any other invocation. The
    lookup defaults to "no existing agent" (rc=0, empty stdout) so create-path
    tests exercise a clean first boot without extra setup.
    """

    def __init__(
        self,
        returncode: int = 0,
        stdout: str = "",
        list_returncode: int = 0,
        list_stdout: str = "",
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.list_returncode = list_returncode
        self.list_stdout = list_stdout
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
        if len(cmd) > 1 and cmd[0] == "mngr" and cmd[1] == "list":
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=self.list_returncode,
                stdout=self.list_stdout,
                stderr="",
            )
        return subprocess.CompletedProcess(
            args=cmd, returncode=self.returncode, stdout=self.stdout, stderr=""
        )

    def commands(self) -> list[str]:
        """The mngr subcommands invoked, in order (e.g. ["list", "create"])."""
        return [cmd[1] for cmd in self.calls if cmd and cmd[0] == "mngr"]


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
    assert stub.commands() == ["list", "create"]
    assert (_bootstrap_env / "runtime" / "initial_chat_created").exists()


def test_maybe_create_initial_chat_persists_created_agent_id(
    monkeypatch: pytest.MonkeyPatch, _bootstrap_env: Path
) -> None:
    """A successful create writes the parsed agent id to the welcome-resend sidecar."""
    stub = _StubSubprocess(returncode=0, stdout='{"agent_id": "agent-created"}\n')
    monkeypatch.setattr("bootstrap.manager.subprocess.run", stub.run)
    _maybe_create_initial_chat()
    assert (
        _bootstrap_env / INITIAL_CHAT_AGENT_ID_FILENAME
    ).read_text() == "agent-created"


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
    assert stub.commands() == ["list", "create"]
    assert not (_bootstrap_env / "runtime" / "initial_chat_created").exists()


def test_maybe_create_initial_chat_adopts_agent_from_partial_create(
    monkeypatch: pytest.MonkeyPatch, _bootstrap_env: Path
) -> None:
    """When an agent named after the host already exists (an earlier boot's
    create died after registering it), the retry adopts it -- persisting its id
    for the welcome resend and writing the signal -- instead of re-creating,
    which would fail with a name collision on every boot."""
    stub = _StubSubprocess(list_stdout="agent-partial\n")
    monkeypatch.setattr("bootstrap.manager.subprocess.run", stub.run)
    _maybe_create_initial_chat()
    assert stub.commands() == ["list"]
    assert (
        _bootstrap_env / INITIAL_CHAT_AGENT_ID_FILENAME
    ).read_text() == "agent-partial"
    assert (_bootstrap_env / "runtime" / "initial_chat_created").exists()


def test_maybe_create_initial_chat_creates_when_lookup_fails(
    monkeypatch: pytest.MonkeyPatch, _bootstrap_env: Path
) -> None:
    """A failed adopt-lookup falls back to the plain create path rather than
    aborting the boot's chat-agent creation."""
    stub = _StubSubprocess(returncode=0, list_returncode=1)
    monkeypatch.setattr("bootstrap.manager.subprocess.run", stub.run)
    _maybe_create_initial_chat()
    assert stub.commands() == ["list", "create"]
    assert (_bootstrap_env / "runtime" / "initial_chat_created").exists()


def test_maybe_create_initial_chat_skips_adopt_on_ambiguous_lookup(
    monkeypatch: pytest.MonkeyPatch, _bootstrap_env: Path
) -> None:
    """Multiple same-named agents cannot be disambiguated; fall through to
    create rather than guessing which survivor to adopt."""
    stub = _StubSubprocess(returncode=0, list_stdout="agent-a\nagent-b\n")
    monkeypatch.setattr("bootstrap.manager.subprocess.run", stub.run)
    _maybe_create_initial_chat()
    assert stub.commands() == ["list", "create"]
    assert not (_bootstrap_env / INITIAL_CHAT_AGENT_ID_FILENAME).exists()


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


# --- _create_orphan_runtime_worktree ---


def test_create_orphan_runtime_worktree_creates_empty_orphan_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The helper must add runtime/ as a worktree on a brand-new orphan branch
    (no parent, empty tree) using only plumbing that works on old git -- no
    `git worktree add --orphan` (git >= 2.42), which Lima's Debian 12 lacks."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_in(repo, "init", "-q", "--initial-branch=main")
    _git_in(repo, "config", "user.email", "seed@test.local")
    _git_in(repo, "config", "user.name", "seed")
    (repo / "README.md").write_text("seed\n")
    _git_in(repo, "add", "-A")
    _git_in(repo, "commit", "-qm", "seed")

    monkeypatch.chdir(repo)
    result = _create_orphan_runtime_worktree("mindsbackup/agent-test")
    assert result.returncode == 0, result.stderr

    runtime = repo / "runtime"
    assert (runtime / ".git").exists()
    # The branch is an orphan: its tip commit has no parents.
    parents = _git_in(runtime, "rev-list", "--parents", "-1", "HEAD").stdout.split()
    assert len(parents) == 1  # just the commit sha, no parent sha
    # Nothing is tracked yet (empty tree); the worktree has no repo content.
    assert _git_in(runtime, "ls-files").stdout.strip() == ""
    assert not (runtime / "README.md").exists()


# --- _apply_container_timezone ---


def _make_zoneinfo_tree(tmp_path: Path) -> Path:
    """Build a fake zoneinfo dir with a single America/New_York zone file."""
    zoneinfo = tmp_path / "zoneinfo"
    (zoneinfo / "America").mkdir(parents=True)
    (zoneinfo / "America" / "New_York").write_bytes(b"TZif-fake")
    return zoneinfo


def test_apply_container_timezone_symlinks_and_writes_name(tmp_path: Path) -> None:
    zoneinfo = _make_zoneinfo_tree(tmp_path)
    etc = tmp_path / "etc"
    etc.mkdir()
    localtime = etc / "localtime"
    timezone_file = etc / "timezone"

    assert _apply_container_timezone(
        "America/New_York",
        zoneinfo_dir=zoneinfo,
        localtime_path=localtime,
        timezone_path=timezone_file,
    )

    assert localtime.is_symlink()
    assert Path(os.readlink(localtime)) == zoneinfo / "America" / "New_York"
    assert timezone_file.read_text() == "America/New_York\n"


def test_apply_container_timezone_replaces_existing_localtime(tmp_path: Path) -> None:
    """The common container case: /etc/localtime already exists (a regular file
    baked into the image) and must be atomically replaced by the symlink."""
    zoneinfo = _make_zoneinfo_tree(tmp_path)
    etc = tmp_path / "etc"
    etc.mkdir()
    localtime = etc / "localtime"
    localtime.write_bytes(b"stale UTC zone data")
    timezone_file = etc / "timezone"

    assert _apply_container_timezone(
        "America/New_York",
        zoneinfo_dir=zoneinfo,
        localtime_path=localtime,
        timezone_path=timezone_file,
    )
    assert localtime.is_symlink()
    assert Path(os.readlink(localtime)) == zoneinfo / "America" / "New_York"


@pytest.mark.parametrize(
    "bad_name",
    [
        "",
        "../../etc",
        "America/../../etc/passwd",
        "America/New York",
        "UTC;rm -rf /",
        "/America/New_York",
        "America/",
    ],
)
def test_apply_container_timezone_rejects_malformed_names(
    tmp_path: Path, bad_name: str
) -> None:
    zoneinfo = _make_zoneinfo_tree(tmp_path)
    etc = tmp_path / "etc"
    etc.mkdir()
    localtime = etc / "localtime"

    assert not _apply_container_timezone(
        bad_name,
        zoneinfo_dir=zoneinfo,
        localtime_path=localtime,
        timezone_path=etc / "timezone",
    )
    assert not localtime.exists()


def test_apply_container_timezone_rejects_unknown_zone(tmp_path: Path) -> None:
    """A well-formed name whose zoneinfo file does not exist is rejected."""
    zoneinfo = _make_zoneinfo_tree(tmp_path)
    etc = tmp_path / "etc"
    etc.mkdir()
    localtime = etc / "localtime"

    assert not _apply_container_timezone(
        "Mars/Olympus_Mons",
        zoneinfo_dir=zoneinfo,
        localtime_path=localtime,
        timezone_path=etc / "timezone",
    )
    assert not localtime.exists()


def test_apply_container_timezone_tolerates_oserror(tmp_path: Path) -> None:
    """A failing filesystem write (here: parent dir absent) returns False
    instead of raising -- bootstrap must never die on the timezone step."""
    zoneinfo = _make_zoneinfo_tree(tmp_path)
    missing_dir = tmp_path / "does-not-exist"

    assert not _apply_container_timezone(
        "America/New_York",
        zoneinfo_dir=zoneinfo,
        localtime_path=missing_dir / "localtime",
        timezone_path=missing_dir / "timezone",
    )


# --- _write_agent_env_snapshot ---


def test_write_agent_env_snapshot_quotes_values_and_restricts_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MINDS_TEST_SPACES", "two words")
    monkeypatch.setenv("MINDS_TEST_QUOTES", "he said \"hi\" and 'bye'")
    snapshot = tmp_path / "agent-env"

    _write_agent_env_snapshot(snapshot)

    assert (snapshot.stat().st_mode & 0o777) == 0o600
    parsed: dict[str, str] = {}
    for line in snapshot.read_text().splitlines():
        # Each line must be a shell-parseable `export KEY=<value>` statement.
        words = shlex.split(line)
        assert words[0] == "export"
        key, _, value = words[1].partition("=")
        parsed[key] = value
    assert parsed["MINDS_TEST_SPACES"] == "two words"
    assert parsed["MINDS_TEST_QUOTES"] == "he said \"hi\" and 'bye'"


def test_write_agent_env_snapshot_skips_non_identifier_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # POSIX allows env keys that are not valid shell identifiers (e.g. with a
    # dash); an `export` line for one would break sourcing, so it is skipped.
    monkeypatch.setenv("MINDS-BAD-KEY", "value")
    monkeypatch.setenv("MINDS_GOOD_KEY", "value")
    snapshot = tmp_path / "agent-env"

    _write_agent_env_snapshot(snapshot)

    content = snapshot.read_text()
    assert "MINDS-BAD-KEY" not in content
    assert "export MINDS_GOOD_KEY=value" in content


def test_write_agent_env_snapshot_overwrites_and_retightens_mode(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "agent-env"
    snapshot.write_text("stale\n")
    snapshot.chmod(0o644)

    _write_agent_env_snapshot(snapshot)

    assert (snapshot.stat().st_mode & 0o777) == 0o600
    assert "stale" not in snapshot.read_text()


# --- _fetch_user_timezone ---


def test_fetch_user_timezone_returns_empty_when_gateway_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LATCHKEY_GATEWAY", raising=False)
    monkeypatch.delenv("LATCHKEY_GATEWAY_PASSWORD", raising=False)
    monkeypatch.delenv("LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE", raising=False)
    assert _fetch_user_timezone() == ""


def test_fallback_timezone_places_local_clock_at_setup_hour() -> None:
    """Every UTC hour maps to a real zone whose local time at setup is 19:xx."""
    for hour in range(24):
        now = datetime(2026, 7, 7, hour, 30, tzinfo=timezone.utc)
        zone_name = _fallback_timezone_for_unknown(now)
        local = now.astimezone(ZoneInfo(zone_name))
        assert local.hour == 19, f"utc hour {hour} -> {zone_name} -> local {local.hour}"


def test_seed_caretaker_stamp_writes_today_for_zone(tmp_path: Path) -> None:
    """First boot writes today's local date so run_daily_job.sh skips the creation day."""
    stamp = tmp_path / "stamps" / "caretaker"
    # 01:30 UTC on the 8th is still the evening of the 7th in Etc/GMT+5 (UTC-5),
    # so the stamp must carry the LOCAL date, not the UTC one.
    now = datetime(2026, 7, 8, 1, 30, tzinfo=timezone.utc)
    _seed_caretaker_stamp("Etc/GMT+5", stamp_path=stamp, now_utc=now)
    assert stamp.read_text() == "2026-07-07\n"


def test_seed_caretaker_stamp_uses_utc_when_zone_empty_or_bad(tmp_path: Path) -> None:
    now = datetime(2026, 7, 8, 1, 30, tzinfo=timezone.utc)
    for bad_zone in ("", "Not/AZone"):
        stamp = (
            tmp_path / f"stamps-{bad_zone or 'empty'}".replace("/", "_") / "caretaker"
        )
        _seed_caretaker_stamp(bad_zone, stamp_path=stamp, now_utc=now)
        assert stamp.read_text() == "2026-07-08\n"


def test_seed_caretaker_stamp_leaves_existing_stamp_alone(tmp_path: Path) -> None:
    """Later boots must not touch the daily-job runner's own state."""
    stamp = tmp_path / "caretaker"
    stamp.write_text("2020-01-01\n")
    _seed_caretaker_stamp("UTC", stamp_path=stamp)
    assert stamp.read_text() == "2020-01-01\n"
    # A pre-marker workspace gets the marker backfilled so later boots
    # short-circuit on it rather than on the stamp's presence.
    assert (tmp_path / "caretaker.seeded").exists()


def test_seed_caretaker_stamp_writes_marker_on_first_seed(tmp_path: Path) -> None:
    stamp = tmp_path / "stamps" / "caretaker"
    _seed_caretaker_stamp("UTC", stamp_path=stamp)
    assert (tmp_path / "stamps" / "caretaker.seeded").exists()


def test_seed_caretaker_stamp_does_not_reseed_deleted_stamp(tmp_path: Path) -> None:
    """A deliberately deleted stamp (forcing a run today) must survive a reboot.

    Stamp absence is the operator's tool for scheduling a same-day run; the
    one-time marker is what records 'this workspace was already seeded', so a
    reboot between the deletion and the due hour cannot re-seed today's date
    and silently cancel the forced run.
    """
    stamp = tmp_path / "stamps" / "caretaker"
    _seed_caretaker_stamp("UTC", stamp_path=stamp)
    stamp.unlink()
    _seed_caretaker_stamp("UTC", stamp_path=stamp)
    assert not stamp.exists()
