"""Unit tests for the github-sync runner.

Covers the stale-index-lock recovery (an interrupted commit must not wedge
every future tick) and full ticks against a local bare origin: commit+push
when the repo is confirmed private, and the push halt when it is public or
unverifiable.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from github_sync.runner import (
    STALE_LOCK_MIN_AGE_SECONDS,
    _clear_stale_index_lock,
    _do_tick,
    _SyncState,
    status_file_path,
)
from github_sync.testing import init_repo, init_repo_with_origin, install_fake_latchkey
from github_sync.worktree import init_runtime_worktree


def _age_lock(lock_path: Path) -> None:
    """Backdate a lock file's mtime so it counts as stale (past the age guard)."""
    old = time.time() - STALE_LOCK_MIN_AGE_SECONDS - 60
    os.utime(lock_path, (old, old))


def _git_out(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )


def test_clear_stale_index_lock_removes_aged_lock_in_linked_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Faithful to production: runtime/ is a linked worktree, so the lock
    lives in the per-worktree git dir, not in a top-level .git/."""
    monkeypatch.chdir(tmp_path)
    main = tmp_path / "main"
    init_repo(main)
    (main / "seed.txt").write_text("seed\n")
    _git_out(main, "add", "-A")
    _git_out(main, "commit", "-qm", "seed")
    # The runner resolves runtime/ relative to its cwd, so the worktree must
    # be named exactly "runtime" and sit in tmp_path.
    _git_out(main, "worktree", "add", str(tmp_path / "runtime"), "-b", "sync")

    lock_path = main / ".git" / "worktrees" / "runtime" / "index.lock"
    lock_path.write_text("")
    _age_lock(lock_path)
    assert lock_path.exists()

    _clear_stale_index_lock()

    assert not lock_path.exists()


def test_clear_stale_index_lock_keeps_fresh_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recently-created lock might belong to a live git operation, so it
    must be left alone rather than yanked out from under that operation."""
    monkeypatch.chdir(tmp_path)
    runtime = tmp_path / "runtime"
    init_repo(runtime)
    lock_path = runtime / ".git" / "index.lock"
    lock_path.write_text("")  # fresh: mtime is now

    _clear_stale_index_lock()

    assert lock_path.exists()


def test_clear_stale_index_lock_noop_when_no_lock_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    init_repo(tmp_path / "runtime")
    # No lock and no git repo problems -- must simply not raise.
    _clear_stale_index_lock()


def test_clear_stale_index_lock_noop_when_runtime_not_a_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runtime").mkdir()
    # `git rev-parse` fails; the function must silently return.
    _clear_stale_index_lock()


def _set_up_synced_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    """A workspace with a local bare origin, a runtime worktree, and sync config."""
    main, origin = init_repo_with_origin(tmp_path)
    monkeypatch.chdir(main)
    (main / "github_sync.toml").write_text(
        'repo_url = "https://github.com/some-user/my-workspace"\n'
    )
    assert init_runtime_worktree() is True
    return main, origin


def test_do_tick_commits_and_pushes_when_repo_private(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git_and_gateway_env: Path,
    fake_latchkey_bin: Path,
) -> None:
    """End to end against a local bare origin: new runtime state is committed
    on runtime-sync and pushed once the repo is confirmed private."""
    main, origin = _set_up_synced_workspace(tmp_path, monkeypatch)
    install_fake_latchkey(fake_latchkey_bin, 'echo \'{"private": true}\'')
    (main / "runtime" / "memory.txt").write_text("important state\n")
    state = _SyncState()

    _do_tick(state)

    remote_log = _git_out(origin, "log", "--oneline", "runtime-sync")
    assert remote_log.returncode == 0
    assert "runtime sync:" in remote_log.stdout
    assert state.last_push_ok is True
    status = json.loads(status_file_path().read_text())
    assert status["is_push_allowed"] is True
    assert status["visibility"] == "private"
    assert status["last_push_ok"] is True


def test_do_tick_self_heals_stale_index_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git_and_gateway_env: Path,
    fake_latchkey_bin: Path,
) -> None:
    """A stale index.lock from a killed prior tick must not wedge syncing:
    the next tick clears it and commits the pending runtime state."""
    main, _ = _set_up_synced_workspace(tmp_path, monkeypatch)
    install_fake_latchkey(fake_latchkey_bin, 'echo \'{"private": true}\'')
    lock_path = main / ".git" / "worktrees" / "runtime" / "index.lock"
    lock_path.write_text("")
    _age_lock(lock_path)
    (main / "runtime" / "memory.txt").write_text("important state\n")

    _do_tick(_SyncState())

    log = _git_out(main / "runtime", "log", "--oneline")
    assert "runtime sync:" in log.stdout


def test_do_tick_halts_pushes_when_repo_public(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git_and_gateway_env: Path,
    fake_latchkey_bin: Path,
) -> None:
    """Public repo => commit locally (nothing is lost) but never push."""
    main, origin = _set_up_synced_workspace(tmp_path, monkeypatch)
    install_fake_latchkey(fake_latchkey_bin, 'echo \'{"private": false}\'')
    (main / "runtime" / "memory.txt").write_text("important state\n")
    state = _SyncState()

    _do_tick(state)

    # Committed locally...
    local_log = _git_out(main / "runtime", "log", "--oneline")
    assert "runtime sync:" in local_log.stdout
    # ...but the branch never reached origin.
    remote_branch = _git_out(origin, "rev-parse", "--verify", "runtime-sync")
    assert remote_branch.returncode != 0
    assert state.is_push_allowed is False
    status = json.loads(status_file_path().read_text())
    assert status["is_push_allowed"] is False
    assert status["visibility"] == "public"


def test_do_tick_holds_pushes_while_visibility_unconfirmed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git_and_gateway_env: Path,
    fake_latchkey_bin: Path,
) -> None:
    """An unreachable visibility check (gateway offline) must fail closed."""
    main, origin = _set_up_synced_workspace(tmp_path, monkeypatch)
    install_fake_latchkey(fake_latchkey_bin, "exit 7")
    (main / "runtime" / "memory.txt").write_text("important state\n")

    _do_tick(_SyncState())

    remote_branch = _git_out(origin, "rev-parse", "--verify", "runtime-sync")
    assert remote_branch.returncode != 0
    status = json.loads(status_file_path().read_text())
    assert status["is_push_allowed"] is False
    assert status["visibility"] == "unknown"


def test_do_tick_idles_when_sync_not_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git_and_gateway_env: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    state = _SyncState()
    _do_tick(state)

    assert state.repo_url is None
    status = json.loads(status_file_path().read_text())
    assert status["repo_url"] is None


def test_do_tick_reports_malformed_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git_and_gateway_env: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "github_sync.toml").write_text("repo_url = [broken")

    state = _SyncState()
    _do_tick(state)

    assert state.last_error is not None
    assert "github_sync.toml" in state.last_error
