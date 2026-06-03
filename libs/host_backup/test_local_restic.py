"""Integration test for host_backup against a local restic repository.

Exercises the full tick loop end-to-end (snapshot in DIRECT mode +
restic init + restic backup + restic forget + restic prune) against a
local `restic` repository in a tmp dir, with no network access.

Skipped automatically when the `restic` binary is not on PATH so this
test still runs cleanly in environments that haven't installed it yet
(restic ships in the FCT Dockerfile + lima provision; CI runners may
not have it).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from host_backup.restic import (
    backup as restic_backup,
)
from host_backup.restic import (
    extract_snapshot_id_from_backup_output,
    init_repo,
    is_repo_missing_error,
    probe_repo,
)
from host_backup.restic import (
    forget as restic_forget,
)
from host_backup.restic import (
    prune as restic_prune,
)


def _restic_available() -> bool:
    return shutil.which("restic") is not None


pytestmark = pytest.mark.skipif(
    not _restic_available(),
    reason="restic binary not on PATH (install via apt-get install restic to enable)",
)


def _env_for_local_repo(repo_path: Path) -> dict[str, str]:
    return {
        "RESTIC_REPOSITORY": str(repo_path),
        "RESTIC_PASSWORD": "integration-test-password",
    }


def test_probe_repo_reports_missing_before_init(tmp_path: Path) -> None:
    env = _env_for_local_repo(tmp_path / "repo")
    probe = probe_repo(env)
    assert probe.returncode != 0
    assert is_repo_missing_error(probe.stderr)


def test_init_then_probe_succeeds(tmp_path: Path) -> None:
    env = _env_for_local_repo(tmp_path / "repo")
    init = init_repo(env)
    assert init.returncode == 0, init.stderr
    probe = probe_repo(env)
    assert probe.returncode == 0


def test_full_backup_forget_prune_cycle(tmp_path: Path) -> None:
    """End-to-end: init, backup, run forget, run prune; check restic snapshots roundtrip."""
    repo_dir = tmp_path / "repo"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "hello.txt").write_text("first")
    (source_dir / "skip-me").mkdir()
    (source_dir / "skip-me" / "junk.txt").write_text("ignored")

    env = _env_for_local_repo(repo_dir)
    init = init_repo(env)
    assert init.returncode == 0, init.stderr

    backup_result = restic_backup(
        source_path=source_dir,
        excludes=("**/skip-me",),
        tag="test-tag",
        env_overrides=env,
    )
    assert backup_result.returncode == 0, backup_result.stderr
    snapshot_id = extract_snapshot_id_from_backup_output(backup_result.stdout)
    assert snapshot_id, (
        "expected to parse a snapshot id from restic backup --json output"
    )

    # Restic exposes the snapshot in `snapshots`:
    snapshots = subprocess.run(
        ["restic", "snapshots", "--json"],
        env={**env, "PATH": _path_for_subprocess()},
        capture_output=True,
        text=True,
        check=True,
    )
    assert snapshot_id in snapshots.stdout

    # Make a second backup so forget has multiple snapshots to consider.
    (source_dir / "hello.txt").write_text("second")
    second_backup = restic_backup(
        source_path=source_dir,
        excludes=("**/skip-me",),
        tag="test-tag-2",
        env_overrides=env,
    )
    assert second_backup.returncode == 0, second_backup.stderr

    forget_result = restic_forget(
        keep_hourly=1,
        keep_daily=1,
        keep_weekly=1,
        keep_monthly=1,
        env_overrides=env,
    )
    assert forget_result.returncode == 0, forget_result.stderr

    prune_result = restic_prune(env)
    assert prune_result.returncode == 0, prune_result.stderr


def test_exclude_pattern_actually_skips_files(tmp_path: Path) -> None:
    """`restic backup --exclude=<glob>` must drop matching files from the snapshot."""
    repo_dir = tmp_path / "repo"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "keep.txt").write_text("kept")
    (source_dir / ".venv").mkdir()
    (source_dir / ".venv" / "should-not-be-backed-up.txt").write_text("excluded")

    env = _env_for_local_repo(repo_dir)
    init_result = init_repo(env)
    assert init_result.returncode == 0

    backup_result = restic_backup(
        source_path=source_dir,
        excludes=("**/.venv",),
        tag="exclude-test",
        env_overrides=env,
    )
    assert backup_result.returncode == 0, backup_result.stderr

    # Listing the snapshot's files via `restic ls latest` must NOT include .venv:
    listing = subprocess.run(
        ["restic", "ls", "latest"],
        env={**env, "PATH": _path_for_subprocess()},
        capture_output=True,
        text=True,
        check=True,
    )
    assert "keep.txt" in listing.stdout
    assert ".venv" not in listing.stdout


def _path_for_subprocess() -> str:
    """Return $PATH for subprocess.run; subprocess clears it when env= is set."""
    return os.environ.get("PATH", "/usr/bin:/bin")
