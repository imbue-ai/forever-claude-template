"""Unit tests for the github-sync runner.

Covers the stale-index-lock recovery (an interrupted commit must not wedge
every future tick), the visibility re-check policy (a confirmed answer is
cached for the check interval; a failed re-check keeps the last confirmed
answer and retries next tick), and full ticks against a local bare origin:
commit+push when the repo is confirmed private, and the push halt when it is
public or unverifiable.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from github_sync.runner import (
    STALE_LOCK_MIN_AGE_SECONDS,
    VISIBILITY_CHECK_INTERVAL_SECONDS,
    _clear_stale_index_lock,
    _do_tick,
    _refresh_visibility,
    _SyncState,
    status_file_path,
)
from github_sync.testing import (
    init_repo,
    init_repo_with_origin,
    install_fake_latchkey,
    run_git,
)
from github_sync.visibility import VISIBILITY_PRIVATE, VISIBILITY_PUBLIC
from github_sync.worktree import init_runtime_worktree, is_runtime_worktree

_REPO_URL = "https://github.com/some-user/my-workspace"


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
    install_fake_latchkey(fake_latchkey_bin, "echo '{\"private\": true}'")
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
    install_fake_latchkey(fake_latchkey_bin, "echo '{\"private\": true}'")
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
    install_fake_latchkey(fake_latchkey_bin, "echo '{\"private\": false}'")
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


def _confirmed_private_state(age_seconds: float) -> _SyncState:
    """A state whose private confirmation is `age_seconds` old."""
    state = _SyncState()
    state.repo_url = _REPO_URL
    state.visibility = VISIBILITY_PRIVATE
    state.visibility_checked_at = datetime.now(timezone.utc) - timedelta(
        seconds=age_seconds
    )
    return state


def test_refresh_visibility_skips_recheck_while_confirmed_answer_is_fresh(
    isolated_git_and_gateway_env: Path, fake_latchkey_bin: Path
) -> None:
    """A confirmed answer younger than the check interval must be trusted
    without re-asking GitHub (the fake would answer public, so any re-check
    would flip the state and fail the assertion)."""
    install_fake_latchkey(fake_latchkey_bin, "echo '{\"private\": false}'")
    state = _confirmed_private_state(age_seconds=1)
    checked_at_before = state.visibility_checked_at

    _refresh_visibility(state, _REPO_URL)

    assert state.visibility == VISIBILITY_PRIVATE
    assert state.is_push_allowed is True
    assert state.visibility_checked_at == checked_at_before


def test_refresh_visibility_keeps_last_confirmed_answer_when_recheck_fails(
    isolated_git_and_gateway_env: Path, fake_latchkey_bin: Path
) -> None:
    """A stale confirmation whose re-check fails outright (gateway offline)
    must keep the last confirmed answer -- pushes would fail too in that
    state, so halting adds nothing -- and leave checked_at unchanged so the
    re-check is retried on the next tick rather than in 15 minutes."""
    install_fake_latchkey(fake_latchkey_bin, "exit 7")
    state = _confirmed_private_state(age_seconds=VISIBILITY_CHECK_INTERVAL_SECONDS + 60)
    checked_at_before = state.visibility_checked_at

    _refresh_visibility(state, _REPO_URL)

    assert state.visibility == VISIBILITY_PRIVATE
    assert state.is_push_allowed is True
    assert state.visibility_checked_at == checked_at_before


def test_refresh_visibility_rechecks_stale_answer_and_halts_on_public(
    isolated_git_and_gateway_env: Path, fake_latchkey_bin: Path
) -> None:
    """Once the confirmed answer goes stale, a re-check happens and a repo
    that flipped public halts pushes."""
    install_fake_latchkey(fake_latchkey_bin, "echo '{\"private\": false}'")
    state = _confirmed_private_state(age_seconds=VISIBILITY_CHECK_INTERVAL_SECONDS + 60)
    checked_at_before = state.visibility_checked_at
    assert checked_at_before is not None

    _refresh_visibility(state, _REPO_URL)

    assert state.visibility == VISIBILITY_PUBLIC
    assert state.is_push_allowed is False
    assert state.visibility_checked_at is not None
    assert state.visibility_checked_at > checked_at_before


def test_do_tick_rechecks_visibility_when_repo_url_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git_and_gateway_env: Path,
    fake_latchkey_bin: Path,
) -> None:
    """A repointed github_sync.toml must not inherit the previous repo's
    confirmed-private answer: the new repo has to earn its own confirmation
    before any push (here the re-check fails, so pushes stay held)."""
    main, origin = _set_up_synced_workspace(tmp_path, monkeypatch)
    install_fake_latchkey(fake_latchkey_bin, "exit 7")
    (main / "runtime" / "memory.txt").write_text("important state\n")
    # State carried over from ticks against a different, previously-configured
    # repo, with a still-fresh private confirmation.
    state = _confirmed_private_state(age_seconds=1)
    state.repo_url = "https://github.com/some-user/previous-repo"

    _do_tick(state)

    assert state.repo_url == _REPO_URL
    remote_branch = _git_out(origin, "rev-parse", "--verify", "runtime-sync")
    assert remote_branch.returncode != 0
    status = json.loads(status_file_path().read_text())
    assert status["is_push_allowed"] is False
    assert status["visibility"] == "unknown"


def test_do_tick_restores_missing_worktree_from_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git_and_gateway_env: Path,
    fake_latchkey_bin: Path,
) -> None:
    """The recreated-workspace self-heal path: when runtime/ is not yet a
    worktree, a tick restores it from origin's runtime-sync branch (prior
    memory/tickets come back) and then syncs as usual."""
    # A first workspace creates runtime state and pushes it to the origin.
    first_base = tmp_path / "first"
    first_base.mkdir()
    first_main, origin = init_repo_with_origin(first_base)
    monkeypatch.chdir(first_main)
    assert init_runtime_worktree() is True
    (first_main / "runtime" / "memory.md").write_text("remember me\n")
    run_git(first_main / "runtime", "add", "-A")
    run_git(first_main / "runtime", "commit", "-qm", "state")
    run_git(first_main / "runtime", "push", "--set-upstream", "origin", "runtime-sync")

    # A workspace recreated from the synced repo: github_sync.toml came along
    # with the checkout, but the container-local runtime worktree did not.
    second_main = tmp_path / "second"
    init_repo(second_main)
    (second_main / "seed.txt").write_text("seed\n")
    run_git(second_main, "add", "-A")
    run_git(second_main, "commit", "-qm", "seed")
    run_git(second_main, "remote", "add", "origin", str(origin))
    (second_main / "github_sync.toml").write_text(
        'repo_url = "https://github.com/some-user/my-workspace"\n'
    )
    install_fake_latchkey(fake_latchkey_bin, "echo '{\"private\": true}'")
    monkeypatch.chdir(second_main)
    state = _SyncState()

    _do_tick(state)

    assert is_runtime_worktree()
    assert (second_main / "runtime" / "memory.md").read_text() == "remember me\n"
    status = json.loads(status_file_path().read_text())
    assert status["is_push_allowed"] is True
    assert status["last_push_ok"] is True


def test_do_tick_defers_when_worktree_missing_and_origin_unreachable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git_and_gateway_env: Path,
) -> None:
    """A recreated workspace whose origin cannot be reached yet (e.g. the
    GitHub permissions have not been re-granted) must defer the worktree init
    and report that in the status, not create a fresh orphan branch."""
    main = tmp_path / "main"
    init_repo(main)
    (main / "seed.txt").write_text("seed\n")
    run_git(main, "add", "-A")
    run_git(main, "commit", "-qm", "seed")
    run_git(main, "remote", "add", "origin", str(tmp_path / "does-not-exist.git"))
    (main / "github_sync.toml").write_text(
        'repo_url = "https://github.com/some-user/my-workspace"\n'
    )
    monkeypatch.chdir(main)
    state = _SyncState()

    _do_tick(state)

    assert not is_runtime_worktree()
    assert state.last_error is not None
    assert "deferred" in state.last_error
    status = json.loads(status_file_path().read_text())
    assert "deferred" in status["last_error"]


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
