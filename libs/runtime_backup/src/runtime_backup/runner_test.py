"""Unit tests for the runtime-backup runner.

Focus: the stale-index-lock recovery that keeps an interrupted commit from
permanently wedging every future backup tick, and the age guard that keeps it
from disturbing a genuinely in-progress git operation.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from runtime_backup.runner import (
    STALE_LOCK_MIN_AGE_SECONDS,
    _clear_stale_index_lock,
    _do_tick,
)


def _git(repo: Path, *args: str) -> None:
    """Run a git command in `repo`, raising on failure."""
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _init_repo(repo: Path) -> None:
    """Create a git repo at `repo` with a committer identity configured."""
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "test@test.local")
    _git(repo, "config", "user.name", "test")


def _age_lock(lock_path: Path) -> None:
    """Backdate a lock file's mtime so it counts as stale (past the age guard)."""
    old = time.time() - STALE_LOCK_MIN_AGE_SECONDS - 60
    os.utime(lock_path, (old, old))


def test_clear_stale_index_lock_removes_aged_lock_in_linked_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Faithful to production: runtime/ is a linked worktree, so the lock
    lives in the per-worktree git dir, not in a top-level .git/."""
    monkeypatch.chdir(tmp_path)
    main = tmp_path / "main"
    _init_repo(main)
    (main / "seed.txt").write_text("seed\n")
    _git(main, "add", "-A")
    _git(main, "commit", "-qm", "seed")
    # The runner resolves runtime/ relative to its cwd, so the worktree must
    # be named exactly "runtime" and sit in tmp_path.
    _git(main, "worktree", "add", str(tmp_path / "runtime"), "-b", "backup")

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
    _init_repo(runtime)
    lock_path = runtime / ".git" / "index.lock"
    lock_path.write_text("")  # fresh: mtime is now

    _clear_stale_index_lock()

    assert lock_path.exists()


def test_clear_stale_index_lock_noop_when_no_lock_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _init_repo(tmp_path / "runtime")
    # No lock and no git repo problems -- must simply not raise.
    _clear_stale_index_lock()


def test_clear_stale_index_lock_noop_when_runtime_not_a_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runtime").mkdir()
    # `git rev-parse` fails; the function must silently return.
    _clear_stale_index_lock()


def test_do_tick_self_heals_stale_index_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale index.lock from a killed prior tick must not wedge backups:
    the next tick clears it and commits the pending runtime state."""
    monkeypatch.chdir(tmp_path)
    runtime = tmp_path / "runtime"
    _init_repo(runtime)
    # A leftover lock from a prior tick's git process that was killed; aged
    # past the guard so it is recognized as stale.
    lock_path = runtime / ".git" / "index.lock"
    lock_path.write_text("")
    _age_lock(lock_path)
    # New runtime state waiting to be backed up.
    (runtime / "memory.txt").write_text("important state\n")

    _do_tick(should_push=False)

    log = subprocess.run(
        ["git", "-C", str(runtime), "log", "--oneline"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "runtime backup:" in log.stdout
