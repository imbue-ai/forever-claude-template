"""Shared test helpers for the github_sync test files."""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path


def run_git(repo: Path, *args: str) -> None:
    """Run a git command in `repo`, raising on failure."""
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def init_repo(repo: Path) -> None:
    """Create a git repo at `repo` with a committer identity configured."""
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", "--initial-branch=main", str(repo)],
        check=True,
        capture_output=True,
    )
    run_git(repo, "config", "user.email", "test@test.local")
    run_git(repo, "config", "user.name", "test")


def init_repo_with_origin(base: Path) -> tuple[Path, Path]:
    """Create a seeded main repo at base/main with a bare origin at base/origin.git."""
    main = base / "main"
    init_repo(main)
    (main / "seed.txt").write_text("seed\n")
    run_git(main, "add", "-A")
    run_git(main, "commit", "-qm", "seed")
    origin = base / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(origin)], check=True, capture_output=True
    )
    run_git(main, "remote", "add", "origin", str(origin))
    return main, origin


def install_fake_latchkey(bin_dir: Path, script_body: str) -> None:
    """Install an executable `latchkey` shell script into `bin_dir`.

    The caller prepends `bin_dir` to PATH (via monkeypatch.setenv) so
    check_repo_visibility exercises its real subprocess path against a
    controllable stand-in instead of the real latchkey CLI.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "latchkey"
    script.write_text(f"#!/usr/bin/env bash\n{script_body}\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
