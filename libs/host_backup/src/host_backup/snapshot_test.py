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
    SnapshotCleanupError,
    SnapshotError,
    _list_snapshot_names,
    _parse_snapshot_timestamp,
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
    # Cleanup is a no-op (nothing was snapshotted):
    assert taker.cleanup_after_backup() == ()


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
    fail_after_requests: int | None = None,
    stop_event: threading.Event,
) -> threading.Thread:
    """Background thread that watches `trigger_dir` and produces result.json files.

    If `fail_after_requests` is set, the first N requests succeed (per
    `exit_code`) and every request after that returns exit_code 2, to exercise
    partial-failure handling in keep-N cleanup.
    """
    trigger_dir.mkdir(parents=True, exist_ok=True)
    request_path = trigger_dir / "request.json"
    result_path = trigger_dir / "result.json"

    def _loop() -> None:
        last_request_mtime: float | None = None
        handled = 0
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
                handled += 1
                effective_exit = exit_code
                effective_error = error_message
                if fail_after_requests is not None and handled > fail_after_requests:
                    effective_exit = 2
                    effective_error = "boom"
                response = {
                    "request_id": payload.get("request_id", ""),
                    "operation": payload.get("operation", ""),
                    "exit_code": effective_exit,
                    "stdout": "ok\n" if effective_exit == 0 else "",
                    "stderr": effective_error,
                    "snapshot_path": snapshot_path if effective_exit == 0 else "",
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
        # The read path is a fresh, uniquely-named child of the snapshots dir.
        assert result.read_path.parent == Path("/mngr-snapshots")
        assert result.read_path != Path("/mngr-snapshots/current")
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
        # take_snapshot creates the snapshot directly (no pre-cleanup); the
        # helper responds with exit_code=2, so the snapshot step raises.
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


def test_outer_trigger_take_snapshot_uses_unique_timestamped_names(
    tmp_path: Path,
) -> None:
    """Two successive snapshots must land on distinct, never-reused paths."""
    settings = _outer_trigger_settings(tmp_path / "trigger")
    stop = threading.Event()
    helper = _start_fake_outer_helper(
        tmp_path / "trigger", snapshot_path="", stop_event=stop
    )
    try:
        taker = OuterTriggerSnapshotTaker(settings=settings)
        # Each take_snapshot round-trips through the helper (~1s), so the two
        # microsecond-resolution timestamps are always distinct -- no sleep.
        first = taker.take_snapshot()
        second = taker.take_snapshot()
        assert first.read_path != second.read_path
        assert first.read_path.parent == Path("/mngr-snapshots")
        assert second.read_path.parent == Path("/mngr-snapshots")
        # The helper returned an empty snapshot_path, so the taker falls back to
        # the outer snapshots dir + the timestamped name it generated.
        assert first.snapshot_path.startswith("/mngr-btrfs/snapshots/")
        assert first.snapshot_path != "/mngr-btrfs/snapshots/current"
    finally:
        stop.set()
        helper.join(timeout=2.0)


# --- snapshot name parsing / listing ---


def test_parse_snapshot_timestamp_accepts_iso_now_and_rejects_others() -> None:
    assert _parse_snapshot_timestamp("2026-06-12T03:43:57.123456Z") is not None
    assert _parse_snapshot_timestamp("current") is None
    assert _parse_snapshot_timestamp("2026-06-12") is None
    assert _parse_snapshot_timestamp("") is None


def test_list_snapshot_names_sorts_oldest_first_and_ignores_non_timestamps(
    tmp_path: Path,
) -> None:
    (tmp_path / "2026-06-12T02:00:00.000000Z").mkdir()
    (tmp_path / "2026-06-12T00:00:00.000000Z").mkdir()
    (tmp_path / "2026-06-12T01:00:00.000000Z").mkdir()
    # Non-timestamp entries must be ignored, never selected for deletion.
    (tmp_path / "current").mkdir()
    (tmp_path / "scratch").mkdir()

    names = _list_snapshot_names(tmp_path)

    assert names == [
        "2026-06-12T00:00:00.000000Z",
        "2026-06-12T01:00:00.000000Z",
        "2026-06-12T02:00:00.000000Z",
    ]


def test_list_snapshot_names_returns_empty_when_dir_missing(tmp_path: Path) -> None:
    assert _list_snapshot_names(tmp_path / "does-not-exist") == []


# --- OuterTriggerSnapshotTaker.cleanup_after_backup (keep-N GC) ---


def _gc_settings(tmp_path: Path, *, max_local_snapshots: int) -> SnapshotSettings:
    # snapshot_read_path's parent is the real, populated read dir we enumerate.
    return SnapshotSettings(
        method=SnapshotMethod.OUTER_TRIGGER,
        btrfs_mount_path=Path("/mngr-btrfs"),
        host_subvolume_path=Path("/mngr-btrfs/abcdef"),
        snapshot_current_path=Path("/mngr-btrfs/snapshots/current"),
        snapshot_read_path=tmp_path / "snapshots" / "current",
        trigger_dir=tmp_path / "trigger",
        outer_helper_timeout_seconds=10.0,
        max_local_snapshots=max_local_snapshots,
    )


def _make_snapshot_dirs(read_dir: Path, names: tuple[str, ...]) -> None:
    read_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (read_dir / name).mkdir()


def test_cleanup_after_backup_deletes_oldest_beyond_cap(tmp_path: Path) -> None:
    settings = _gc_settings(tmp_path, max_local_snapshots=2)
    names = (
        "2026-06-12T00:00:00.000000Z",
        "2026-06-12T01:00:00.000000Z",
        "2026-06-12T02:00:00.000000Z",
        "2026-06-12T03:00:00.000000Z",
    )
    _make_snapshot_dirs(tmp_path / "snapshots", names)
    stop = threading.Event()
    helper = _start_fake_outer_helper(tmp_path / "trigger", stop_event=stop)
    try:
        taker = OuterTriggerSnapshotTaker(settings=settings)
        deleted = taker.cleanup_after_backup()
        # The two oldest are deleted; the newest two are kept.
        assert deleted == (
            "/mngr-btrfs/snapshots/2026-06-12T00:00:00.000000Z",
            "/mngr-btrfs/snapshots/2026-06-12T01:00:00.000000Z",
        )
    finally:
        stop.set()
        helper.join(timeout=2.0)


def test_cleanup_after_backup_is_noop_when_at_or_under_cap(tmp_path: Path) -> None:
    settings = _gc_settings(tmp_path, max_local_snapshots=5)
    _make_snapshot_dirs(
        tmp_path / "snapshots",
        ("2026-06-12T00:00:00.000000Z", "2026-06-12T01:00:00.000000Z"),
    )
    taker = OuterTriggerSnapshotTaker(settings=settings)
    # No helper needed: under the cap, no cleanup requests are sent.
    assert taker.cleanup_after_backup() == ()


def test_cleanup_after_backup_raises_when_helper_fails(tmp_path: Path) -> None:
    settings = _gc_settings(tmp_path, max_local_snapshots=1)
    _make_snapshot_dirs(
        tmp_path / "snapshots",
        ("2026-06-12T00:00:00.000000Z", "2026-06-12T01:00:00.000000Z"),
    )
    stop = threading.Event()
    helper = _start_fake_outer_helper(
        tmp_path / "trigger", exit_code=2, error_message="boom", stop_event=stop
    )
    try:
        taker = OuterTriggerSnapshotTaker(settings=settings)
        with pytest.raises(SnapshotError) as excinfo:
            taker.cleanup_after_backup()
        assert "rc=2" in str(excinfo.value)
    finally:
        stop.set()
        helper.join(timeout=2.0)


def test_cleanup_after_backup_partial_failure_reports_deleted_and_failed(
    tmp_path: Path,
) -> None:
    """A mid-way cleanup failure surfaces what was deleted and which target failed."""
    settings = _gc_settings(tmp_path, max_local_snapshots=1)
    names = (
        "2026-06-12T00:00:00.000000Z",
        "2026-06-12T01:00:00.000000Z",
        "2026-06-12T02:00:00.000000Z",
    )
    _make_snapshot_dirs(tmp_path / "snapshots", names)
    stop = threading.Event()
    # First cleanup (oldest) succeeds; the second fails.
    helper = _start_fake_outer_helper(
        tmp_path / "trigger", fail_after_requests=1, stop_event=stop
    )
    try:
        taker = OuterTriggerSnapshotTaker(settings=settings)
        with pytest.raises(SnapshotCleanupError) as excinfo:
            taker.cleanup_after_backup()
        err = excinfo.value
        assert err.deleted == ("/mngr-btrfs/snapshots/2026-06-12T00:00:00.000000Z",)
        assert err.failed_target == "/mngr-btrfs/snapshots/2026-06-12T01:00:00.000000Z"
    finally:
        stop.set()
        helper.join(timeout=2.0)
