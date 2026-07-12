"""Unit tests for runtime/ worktree init and restore-from-origin."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from github_sync.testing import init_repo, init_repo_with_origin, run_git
from github_sync.worktree import init_runtime_worktree, is_runtime_worktree


def _git_out(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    ).stdout


def test_init_creates_orphan_worktree_when_origin_has_no_sync_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main, origin = init_repo_with_origin(tmp_path)
    monkeypatch.chdir(main)

    assert init_runtime_worktree() is True

    runtime = main / "runtime"
    assert is_runtime_worktree()
    # The branch is an orphan: a single parentless root, sharing no history
    # with main (whose seed commit must not be reachable).
    roots = _git_out(runtime, "rev-list", "--max-parents=0", "HEAD").split()
    assert len(roots) == 1
    assert "seed" not in _git_out(runtime, "log", "--format=%s")
    assert _git_out(runtime, "branch", "--show-current").strip() == "runtime-sync"
    # Secrets are excluded from the sync branch.
    assert (runtime / ".gitignore").read_text() == "secrets\n"


def test_init_stages_aside_and_restores_preexisting_runtime_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main, origin = init_repo_with_origin(tmp_path)
    monkeypatch.chdir(main)
    runtime = main / "runtime"
    runtime.mkdir()
    (runtime / "initial_chat_created").write_text("")

    assert init_runtime_worktree() is True

    assert (runtime / "initial_chat_created").exists()
    assert not (main / "runtime.preexisting").exists()


def test_init_restores_runtime_state_from_origin_sync_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recreated-workspace path: origin already has runtime-sync, so init
    materializes the prior runtime/ state instead of starting fresh."""
    # A first workspace creates and pushes runtime state.
    first_base = tmp_path / "first"
    first_base.mkdir()
    first_main, origin = init_repo_with_origin(first_base)
    monkeypatch.chdir(first_main)
    assert init_runtime_worktree() is True
    (first_main / "runtime" / "memory.md").write_text("remember me\n")
    run_git(first_main / "runtime", "add", "-A")
    run_git(first_main / "runtime", "commit", "-qm", "state")
    run_git(first_main / "runtime", "push", "--set-upstream", "origin", "runtime-sync")

    # A second workspace pointing at the same origin restores that state.
    second_main = tmp_path / "second"
    init_repo(second_main)
    (second_main / "seed.txt").write_text("seed\n")
    run_git(second_main, "add", "-A")
    run_git(second_main, "commit", "-qm", "seed")
    run_git(second_main, "remote", "add", "origin", str(origin))
    # Fresh-boot files already in runtime/ must survive the restore.
    runtime = second_main / "runtime"
    runtime.mkdir()
    (runtime / "initial_chat_created").write_text("")
    monkeypatch.chdir(second_main)

    assert init_runtime_worktree() is True

    assert (runtime / "memory.md").read_text() == "remember me\n"
    assert (runtime / "initial_chat_created").exists()
    upstream = _git_out(
        runtime, "rev-parse", "--abbrev-ref", "runtime-sync@{upstream}"
    ).strip()
    assert upstream == "origin/runtime-sync"


def test_init_defers_when_origin_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With origin unreachable we cannot know whether a remote runtime-sync
    branch exists, so init must NOT create a fresh orphan branch (it could
    permanently diverge from a restorable remote); it defers instead."""
    main = tmp_path / "main"
    init_repo(main)
    (main / "seed.txt").write_text("seed\n")
    run_git(main, "add", "-A")
    run_git(main, "commit", "-qm", "seed")
    run_git(main, "remote", "add", "origin", str(tmp_path / "does-not-exist.git"))
    runtime = main / "runtime"
    runtime.mkdir()
    (runtime / "precious.txt").write_text("keep\n")
    monkeypatch.chdir(main)

    assert init_runtime_worktree() is False

    assert not is_runtime_worktree()
    assert (runtime / "precious.txt").read_text() == "keep\n"


def test_init_reattach_writes_missing_secrets_gitignore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A runtime-sync branch left behind by an interrupted prior init (the
    branch is created just before the `worktree add` that failed) has no
    .gitignore in its history. Reattaching to it must still put the secrets
    exclusion in place, or the next sync tick's `git add -A` would push
    runtime/secrets to the remote."""
    main, origin = init_repo_with_origin(tmp_path)
    monkeypatch.chdir(main)
    # Mirror what the interrupted init leaves: a parentless empty-tree commit
    # on runtime-sync and no worktree.
    empty_tree = _git_out(main, "hash-object", "-w", "-t", "tree", "/dev/null").strip()
    orphan_commit = _git_out(
        main, "commit-tree", empty_tree, "-m", "runtime sync: init"
    ).strip()
    run_git(main, "branch", "runtime-sync", orphan_commit)

    assert init_runtime_worktree() is True

    runtime = main / "runtime"
    assert (runtime / ".gitignore").read_text() == "secrets\n"
    # Committed, not just written: the worktree is clean afterwards.
    assert _git_out(runtime, "status", "--porcelain").strip() == ""


def test_init_noops_when_already_a_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main, origin = init_repo_with_origin(tmp_path)
    monkeypatch.chdir(main)
    assert init_runtime_worktree() is True
    head_before = _git_out(main / "runtime", "rev-parse", "HEAD")

    assert init_runtime_worktree() is True

    assert _git_out(main / "runtime", "rev-parse", "HEAD") == head_before
