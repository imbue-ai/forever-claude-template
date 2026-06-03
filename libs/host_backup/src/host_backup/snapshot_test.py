"""Unit tests for host_backup.snapshot."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from host_backup.config import SnapshotMethod, SnapshotSettings
from host_backup.snapshot import (
    DirectSnapshotTaker,
    OuterTriggerSnapshotTaker,
    SnapshotError,
    make_snapshot_taker,
)

# --- DirectSnapshotTaker ---


def test_direct_snapshot_taker_returns_read_path_from_settings() -> None:
    settings = SnapshotSettings(
        method=SnapshotMethod.DIRECT,
        snapshot_read_path=Path("/mngr"),
    )
    taker = DirectSnapshotTaker(settings=settings)
    result = taker.take_snapshot()
    assert result.method == SnapshotMethod.DIRECT
    assert result.read_path == Path("/mngr")
    # Delete is a no-op:
    taker.delete_snapshot()


def test_direct_snapshot_taker_defaults_read_path_when_unset() -> None:
    taker = DirectSnapshotTaker(settings=SnapshotSettings(method=SnapshotMethod.DIRECT))
    result = taker.take_snapshot()
    assert result.read_path == Path("/mngr")


# --- make_snapshot_taker ---


def test_make_snapshot_taker_raises_when_outer_trigger_missing_required_paths() -> None:
    bad_settings = SnapshotSettings(method=SnapshotMethod.OUTER_TRIGGER)
    with pytest.raises(SnapshotError):
        make_snapshot_taker(bad_settings)


def test_make_snapshot_taker_raises_when_btrfs_local_missing_paths() -> None:
    bad_settings = SnapshotSettings(method=SnapshotMethod.BTRFS_LOCAL)
    with pytest.raises(SnapshotError):
        make_snapshot_taker(bad_settings)


# --- OuterTriggerSnapshotTaker (faked outer helper) ---


def _outer_trigger_settings(trigger_dir: Path) -> SnapshotSettings:
    return SnapshotSettings(
        method=SnapshotMethod.OUTER_TRIGGER,
        btrfs_mount_path=Path("/mngr-btrfs"),
        host_subvolume_path=Path("/mngr-btrfs/abcdef"),
        snapshot_current_path=Path("/mngr-btrfs/snapshots/current"),
        snapshot_read_path=Path("/mngr-snapshots/current"),
        trigger_dir=trigger_dir,
        outer_helper_timeout_seconds=10.0,
    )


def _start_fake_outer_helper(
    trigger_dir: Path,
    *,
    snapshot_path: str = "/mngr-btrfs/snapshots/current",
    exit_code: int = 0,
    error_message: str = "",
    stop_event: threading.Event,
) -> threading.Thread:
    """Background thread that watches `trigger_dir` and produces result.json files."""
    trigger_dir.mkdir(parents=True, exist_ok=True)
    request_path = trigger_dir / "request.json"
    result_path = trigger_dir / "result.json"

    def _loop() -> None:
        last_request_mtime: float | None = None
        while not stop_event.is_set():
            try:
                mtime = request_path.stat().st_mtime
            except OSError:
                mtime = None
            if mtime is not None and mtime != last_request_mtime:
                last_request_mtime = mtime
                try:
                    payload = json.loads(request_path.read_text())
                except (OSError, ValueError):
                    continue
                response = {
                    "request_id": payload.get("request_id", ""),
                    "operation": payload.get("operation", ""),
                    "exit_code": exit_code,
                    "stdout": "ok\n" if exit_code == 0 else "",
                    "stderr": error_message,
                    "snapshot_path": snapshot_path if exit_code == 0 else "",
                }
                tmp = trigger_dir / "result.json.tmp"
                tmp.write_text(json.dumps(response))
                tmp.replace(result_path)
            time.sleep(0.05)

    thread = threading.Thread(target=_loop, daemon=True)
    thread.start()
    return thread


def test_outer_trigger_snapshot_takes_then_returns_result(tmp_path: Path) -> None:
    settings = _outer_trigger_settings(tmp_path / "trigger")
    stop = threading.Event()
    helper = _start_fake_outer_helper(tmp_path / "trigger", stop_event=stop)
    try:
        taker = OuterTriggerSnapshotTaker(settings=settings)
        result = taker.take_snapshot()
        assert result.method == SnapshotMethod.OUTER_TRIGGER
        assert result.read_path == Path("/mngr-snapshots/current")
        assert result.snapshot_path == "/mngr-btrfs/snapshots/current"
        assert result.helper_exit_code == 0
    finally:
        stop.set()
        helper.join(timeout=2.0)


def test_outer_trigger_snapshot_propagates_helper_failure(tmp_path: Path) -> None:
    settings = _outer_trigger_settings(tmp_path / "trigger")
    stop = threading.Event()
    helper = _start_fake_outer_helper(
        tmp_path / "trigger",
        exit_code=2,
        error_message="boom",
        stop_event=stop,
    )
    try:
        taker = OuterTriggerSnapshotTaker(settings=settings)
        with pytest.raises(SnapshotError) as excinfo:
            taker.take_snapshot()
        # The very first thing the taker does is `cleanup`; the helper
        # responds with exit_code=2 for that, so cleanup raises first.
        assert "rc=2" in str(excinfo.value)
    finally:
        stop.set()
        helper.join(timeout=2.0)


def test_outer_trigger_snapshot_times_out_when_no_helper_responds(
    tmp_path: Path,
) -> None:
    settings = SnapshotSettings(
        method=SnapshotMethod.OUTER_TRIGGER,
        btrfs_mount_path=Path("/mngr-btrfs"),
        host_subvolume_path=Path("/mngr-btrfs/abcdef"),
        snapshot_current_path=Path("/mngr-btrfs/snapshots/current"),
        snapshot_read_path=Path("/mngr-snapshots/current"),
        trigger_dir=tmp_path / "trigger",
        outer_helper_timeout_seconds=1.0,
    )
    taker = OuterTriggerSnapshotTaker(settings=settings)
    with pytest.raises(SnapshotError) as excinfo:
        taker.take_snapshot()
    assert "Timed out" in str(excinfo.value)


def test_outer_trigger_writes_request_atomically(tmp_path: Path) -> None:
    settings = _outer_trigger_settings(tmp_path / "trigger")
    stop = threading.Event()
    helper = _start_fake_outer_helper(tmp_path / "trigger", stop_event=stop)
    try:
        taker = OuterTriggerSnapshotTaker(settings=settings)
        taker.take_snapshot()
        # No leftover tmp file from atomic rename:
        assert not (tmp_path / "trigger" / "request.json.tmp").exists()
        # The final request.json is parseable JSON:
        request = json.loads((tmp_path / "trigger" / "request.json").read_text())
        assert "request_id" in request
        assert request["operation"] in {"snapshot", "cleanup"}
    finally:
        stop.set()
        helper.join(timeout=2.0)
