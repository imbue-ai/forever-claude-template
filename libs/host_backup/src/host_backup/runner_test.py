"""Unit tests for host_backup.runner pure-logic helpers."""

from __future__ import annotations

import time

from host_backup.config import BackupConfig, SnapshotMethod, SnapshotSettings
from host_backup.runner import _LoopState, _should_tick_now


def _build_config(
    *,
    backup_interval_seconds: float = 3600.0,
    minimum_backup_gap_seconds: float = 60.0,
) -> BackupConfig:
    return BackupConfig(
        backup_interval_seconds=backup_interval_seconds,
        minimum_backup_gap_seconds=minimum_backup_gap_seconds,
        snapshot=SnapshotSettings(method=SnapshotMethod.DIRECT),
        restic={
            "repository_url_template": "s3:foo/{host_id}",
            "template_values": {},
        },
    )


def test_should_tick_now_fires_on_startup() -> None:
    state = _LoopState()
    config = _build_config()
    decision, reason = _should_tick_now(
        state=state, config=config, backup_mtime=None, env_mtime=None
    )
    assert decision is True
    assert reason == "startup"


def test_should_tick_now_refuses_during_min_gap() -> None:
    state = _LoopState()
    state.last_tick_end_monotonic = time.monotonic()
    config = _build_config(minimum_backup_gap_seconds=60.0)
    decision, reason = _should_tick_now(
        state=state, config=config, backup_mtime=None, env_mtime=None
    )
    assert decision is False
    assert reason == "min_gap_not_elapsed"


def test_should_tick_now_fires_on_config_mtime_change() -> None:
    state = _LoopState()
    state.last_tick_end_monotonic = time.monotonic() - 120.0  # past the min gap
    state.last_backup_toml_mtime = 1000.0
    config = _build_config()
    decision, reason = _should_tick_now(
        state=state, config=config, backup_mtime=2000.0, env_mtime=None
    )
    assert decision is True
    assert reason == "config_change"


def test_should_tick_now_fires_on_env_mtime_change() -> None:
    state = _LoopState()
    state.last_tick_end_monotonic = time.monotonic() - 120.0
    state.last_restic_env_mtime = 500.0
    config = _build_config()
    decision, reason = _should_tick_now(
        state=state, config=config, backup_mtime=None, env_mtime=900.0
    )
    assert decision is True
    assert reason == "config_change"


def test_should_tick_now_refuses_before_interval_elapses() -> None:
    state = _LoopState()
    state.last_tick_end_monotonic = time.monotonic() - 120.0
    state.last_backup_toml_mtime = 1000.0
    state.last_restic_env_mtime = 500.0
    config = _build_config(backup_interval_seconds=3600.0)
    decision, reason = _should_tick_now(
        state=state, config=config, backup_mtime=1000.0, env_mtime=500.0
    )
    assert decision is False
    assert reason == "interval_not_elapsed"


def test_should_tick_now_fires_after_interval_elapses() -> None:
    state = _LoopState()
    # Pretend the prior tick ended 4000 seconds ago (just over the default
    # 3600s interval).
    state.last_tick_end_monotonic = time.monotonic() - 4000.0
    state.last_backup_toml_mtime = 1000.0
    state.last_restic_env_mtime = 500.0
    config = _build_config(backup_interval_seconds=3600.0)
    decision, reason = _should_tick_now(
        state=state, config=config, backup_mtime=1000.0, env_mtime=500.0
    )
    assert decision is True
    assert reason == "interval"


def test_should_tick_now_refuses_when_config_unavailable() -> None:
    state = _LoopState()
    decision, reason = _should_tick_now(
        state=state, config=None, backup_mtime=None, env_mtime=None
    )
    assert decision is False
    assert reason == "no_config"
