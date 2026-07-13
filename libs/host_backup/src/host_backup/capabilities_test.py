"""Unit tests for host_backup.capabilities detection."""

from __future__ import annotations

from pathlib import Path

from host_backup.capabilities import (
    BackupCapabilities,
    SnapshotMethod,
    detect_backup_capabilities,
)


def test_detects_outer_trigger_when_trigger_dir_exists(tmp_path: Path) -> None:
    trigger_dir = tmp_path / "mngr-snapshot"
    trigger_dir.mkdir()
    capabilities = detect_backup_capabilities(
        trigger_dir=trigger_dir, host_dir=tmp_path / "mngr"
    )
    assert capabilities.method == SnapshotMethod.OUTER_TRIGGER
    assert capabilities.trigger_dir == trigger_dir
    assert capabilities.snapshot_read_path == Path("/mngr-snapshots/current")


def test_detects_direct_when_no_trigger_dir_and_not_btrfs(tmp_path: Path) -> None:
    # tmp_path lives on the test runner's ordinary filesystem (not btrfs), so
    # detection falls through to DIRECT and restic reads the host_dir live.
    host_dir = tmp_path / "mngr"
    host_dir.mkdir()
    capabilities = detect_backup_capabilities(
        trigger_dir=tmp_path / "absent-trigger", host_dir=host_dir
    )
    assert capabilities.method == SnapshotMethod.DIRECT
    assert capabilities.snapshot_read_path == host_dir
    assert capabilities.trigger_dir is None


def test_trigger_dir_that_is_a_file_does_not_count(tmp_path: Path) -> None:
    trigger_path = tmp_path / "mngr-snapshot"
    trigger_path.write_text("not a directory")
    host_dir = tmp_path / "mngr"
    host_dir.mkdir()
    capabilities = detect_backup_capabilities(
        trigger_dir=trigger_path, host_dir=host_dir
    )
    assert capabilities.method == SnapshotMethod.DIRECT


def test_capabilities_model_defaults() -> None:
    capabilities = BackupCapabilities(method=SnapshotMethod.DIRECT)
    assert capabilities.outer_helper_timeout_seconds == 120.0
    assert capabilities.max_local_snapshots == 5
