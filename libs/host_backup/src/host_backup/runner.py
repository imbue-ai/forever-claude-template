"""Long-running tick loop for the host_backup service.

Owns the main loop, config-reload state machine, and per-tick orchestration.
The actual restic and snapshot mechanics live in `restic.py` and `snapshot.py`.
"""

import subprocess
import time
import traceback
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Final
from uuid import uuid4

from loguru import logger

from host_backup.capabilities import BackupCapabilities, detect_backup_capabilities
from host_backup.config import (
    BACKUP_TOML_PATH,
    PRUNE_TIMESTAMP_PATH,
    RESTIC_ENV_PATH,
    BackupConfig,
    get_events_dir,
    load_backup_config,
    load_restic_env,
    missing_required_restic_keys,
    publish_service_events_dir,
)
from host_backup.events import BackupEventType, make_event, write_event
from host_backup.restic import backup as restic_backup
from host_backup.restic import (
    extract_snapshot_id_from_backup_output,
    is_repo_locked_error,
)
from host_backup.restic import forget as restic_forget
from host_backup.restic import prune as restic_prune
from host_backup.restic import unlock as restic_unlock
from host_backup.snapshot import (
    SnapshotCleanupError,
    SnapshotError,
    SnapshotResult,
    make_snapshot_taker,
)

LOG_FILE = Path("/tmp/host-backup.log")

# Number of consecutive failed ticks after which the runner raises a prominent,
# durable alarm so a silent multi-day backup outage cannot go unnoticed.
CONSECUTIVE_FAILURE_ALARM_THRESHOLD: Final[int] = 3

# The restic-call signatures the backup step depends on, injected so the
# orchestration can be unit-tested without shelling out to restic.
BackupFn = Callable[..., subprocess.CompletedProcess[str]]
UnlockFn = Callable[..., subprocess.CompletedProcess[str]]


def main() -> None:
    """Entry point: tee logs to disk, detect capabilities, then loop forever."""
    logger.add(LOG_FILE, level="INFO")
    logger.info("Starting host-backup")
    _run_loop(detect_backup_capabilities())


def _run_loop(capabilities: BackupCapabilities) -> None:
    """The actual loop. Extracted for testability."""
    state = _LoopState(capabilities)
    # Publish where this service writes events so `host-backup-now` invoked by a
    # non-primary agent can find it (the service runs under the primary agent's
    # state dir; a non-primary caller cannot derive that path from its own env).
    publish_service_events_dir(state.events_dir)
    logger.info(
        "Detected backup capabilities: method={} (read_path={}, trigger_dir={})",
        capabilities.method.value,
        capabilities.snapshot_read_path,
        capabilities.trigger_dir,
    )
    write_event(
        state.events_dir,
        make_event(
            BackupEventType.CAPABILITIES_DETECTED,
            method=capabilities.method.value,
            snapshot_read_path=str(capabilities.snapshot_read_path),
            trigger_dir=str(capabilities.trigger_dir),
        ),
    )
    while True:
        try:
            _service_iteration(state)
        except KeyboardInterrupt:
            logger.info("Received KeyboardInterrupt; exiting host-backup loop")
            return
        except Exception as e:
            # Defensive top-of-loop catch: any unhandled error inside
            # _service_iteration would crash the service; we'd rather log
            # and keep going. Per spec "the outer service loop never exits".
            logger.opt(exception=e).error("Unhandled error in host-backup loop")
            _emit_tick_error(state, e)
            # Force a min-gap sleep so a deterministic crash loop doesn't pin a CPU.
            time.sleep(_max_safe_gap(state))


class _LoopState:
    """Mutable state carried across iterations of the tick loop."""

    def __init__(self, capabilities: BackupCapabilities) -> None:
        self.capabilities = capabilities
        self.events_dir = get_events_dir()
        self.last_backup_toml_mtime: float | None = None
        self.last_restic_env_mtime: float | None = None
        self.last_tick_end_monotonic: float | None = None
        self.last_known_config: BackupConfig | None = None
        # Mtime of backup.toml when the config was last (re)loaded; used to skip
        # redundant reloads (and their tolerant-parse warnings) between edits.
        self.last_loaded_backup_toml_mtime: float | None = None
        # Per-tick id (reset every iteration); falls back to "no-tick" before the
        # first tick fires so the very first crash still has a correlation id.
        self.current_tick_id: str = "no-tick"
        # Count of ticks that have failed back-to-back (reset to 0 on any
        # successful backup); drives the repeated-failure escalation alarm.
        self.consecutive_backup_failures: int = 0


def _service_iteration(state: _LoopState) -> None:
    """One iteration of the outer poll/run loop.

    Sleeps for `config_poll_interval_seconds`, then asks `_should_tick_now`
    whether the next tick should fire. If yes, runs one tick.
    """
    backup_mtime = _safe_mtime(BACKUP_TOML_PATH)
    env_mtime = _safe_mtime(RESTIC_ENV_PATH)
    config = _load_config_if_changed(state, backup_mtime)
    poll_interval = config.config_poll_interval_seconds

    should_tick, reason = _should_tick_now(
        state=state,
        config=config,
        backup_mtime=backup_mtime,
        env_mtime=env_mtime,
    )

    if not should_tick:
        time.sleep(poll_interval)
        return

    state.current_tick_id = uuid4().hex
    state.last_backup_toml_mtime = backup_mtime
    state.last_restic_env_mtime = env_mtime
    _run_one_tick(state=state, config=config, trigger_reason=reason)
    state.last_tick_end_monotonic = time.monotonic()


def _load_config_if_changed(
    state: _LoopState, backup_mtime: float | None, path: Path = BACKUP_TOML_PATH
) -> BackupConfig:
    """(Re)load backup.toml only when its mtime moved since the last load.

    Tolerant loading never fails, but re-parsing an unchanged file every poll
    would repeat its warnings every few seconds; caching on mtime keeps each
    problem reported once per edit.
    """
    if (
        state.last_known_config is not None
        and backup_mtime == state.last_loaded_backup_toml_mtime
    ):
        return state.last_known_config
    config = load_backup_config(path)
    state.last_known_config = config
    state.last_loaded_backup_toml_mtime = backup_mtime
    return config


def _should_tick_now(
    *,
    state: _LoopState,
    config: BackupConfig,
    backup_mtime: float | None,
    env_mtime: float | None,
) -> tuple[bool, str]:
    """Decide whether to fire a tick now; returns (decision, reason_string).

    Reasons: 'startup' (first tick after process start), 'config_change'
    (either file's mtime differs from last seen -- including the file
    appearing or disappearing, since neither file exists until written:
    minds injects restic.env and host-backup-now may create backup.toml),
    'interval' (the wall-clock backup interval elapsed).
    """
    if state.last_tick_end_monotonic is None:
        return True, "startup"
    elapsed_since_last_tick_end = time.monotonic() - state.last_tick_end_monotonic
    if elapsed_since_last_tick_end < config.minimum_backup_gap_seconds:
        return False, "min_gap_not_elapsed"
    if backup_mtime != state.last_backup_toml_mtime:
        return True, "config_change"
    if env_mtime != state.last_restic_env_mtime:
        return True, "config_change"
    if elapsed_since_last_tick_end >= config.backup_interval_seconds:
        return True, "interval"
    return False, "interval_not_elapsed"


def _run_one_tick(
    *,
    state: _LoopState,
    config: BackupConfig,
    trigger_reason: str,
) -> None:
    """Run one full backup tick.

    Each per-step helper is responsible for emitting its own success or
    failure event and returning a clean signal to this orchestrator. The
    enclosing `_service_iteration` catches anything unexpected at the
    outer loop boundary, so this function deliberately does NOT wrap the
    sequence in a broad try/except.
    """
    tick_id = state.current_tick_id
    backup_mtime = _safe_mtime(BACKUP_TOML_PATH) or 0.0
    env_mtime = _safe_mtime(RESTIC_ENV_PATH)

    write_event(
        state.events_dir,
        make_event(
            BackupEventType.CONFIG_RELOADED,
            tick_id=tick_id,
            backup_toml_mtime=backup_mtime,
            restic_env_mtime=env_mtime,
        ),
    )
    write_event(
        state.events_dir,
        make_event(
            BackupEventType.BACKUP_STARTED,
            tick_id=tick_id,
            trigger_reason=trigger_reason,
        ),
    )

    env = _check_secrets_present(state=state)
    if env is None:
        return
    # restic.env is the overlay restic runs with: RESTIC_REPOSITORY plus every
    # credential restic reads from the environment. The repository is created
    # (and keyed) by the minds app, so host_backup just backs up to it -- it
    # never probes-then-inits the repo itself.
    env_overrides = dict(env)
    snapshot_result = _take_snapshot(state=state)
    if snapshot_result is None:
        return
    try:
        backup_succeeded = _run_restic_backup(
            state=state,
            config=config,
            snapshot=snapshot_result,
            env_overrides=env_overrides,
        )
    finally:
        _cleanup_snapshot(state=state, snapshot=snapshot_result)
    if not backup_succeeded:
        return
    _run_forget(state=state, config=config, env_overrides=env_overrides)
    _maybe_run_prune(state=state, config=config, env_overrides=env_overrides)


# ---------------------------------------------------------------------------
# Per-step helpers
# ---------------------------------------------------------------------------


def _check_secrets_present(*, state: _LoopState) -> dict[str, str] | None:
    """Load restic.env and confirm all required keys are non-empty."""
    env = load_restic_env()
    missing = missing_required_restic_keys(env)
    if missing:
        write_event(
            state.events_dir,
            make_event(
                BackupEventType.TICK_SKIPPED_DUE_TO_MISSING_SECRETS,
                tick_id=state.current_tick_id,
                missing_keys=tuple(missing),
            ),
        )
        logger.warning("Skipping tick: missing required restic.env keys: {}", missing)
        return None
    return env


def _take_snapshot(*, state: _LoopState) -> SnapshotResult | None:
    """Build the snapshot taker and call it; emit SNAPSHOT_CREATED on success."""
    try:
        taker = make_snapshot_taker(state.capabilities)
        result = taker.take_snapshot()
    except SnapshotError as e:
        # A failed snapshot aborts the whole tick before restic runs, so surface
        # it as a structured event (parity with RESTIC_BACKUP_FAILED) rather than
        # only an ephemeral log line -- otherwise a non-zero helper result.json is
        # invisible in the durable events stream.
        logger.error("Snapshot step failed: {}", e)
        write_event(
            state.events_dir,
            make_event(
                BackupEventType.SNAPSHOT_FAILED,
                tick_id=state.current_tick_id,
                method=state.capabilities.method.value,
                error_message=str(e),
            ),
        )
        return None
    write_event(
        state.events_dir,
        make_event(
            BackupEventType.SNAPSHOT_CREATED,
            tick_id=state.current_tick_id,
            method=result.method.value,
            snapshot_path=result.snapshot_path,
            duration_seconds=result.duration_seconds,
            helper_exit_code=result.helper_exit_code,
            helper_stdout=result.helper_stdout,
            helper_stderr=result.helper_stderr,
        ),
    )
    return result


def _cleanup_snapshot(*, state: _LoopState, snapshot: SnapshotResult) -> None:
    """Reclaim snapshots after the backup; emit one SNAPSHOT_DELETED per deletion.

    For outer_trigger this prunes old snapshots down to max_local_snapshots; for
    btrfs_local it deletes the single `current` snapshot; for direct it is a
    no-op (and emits nothing).
    """
    try:
        taker = make_snapshot_taker(state.capabilities)
        deleted_paths = taker.cleanup_after_backup()
    except SnapshotCleanupError as e:
        # A keep-N cleanup failed partway: log the deletions that did succeed,
        # then a failure event naming the exact snapshot whose deletion failed.
        logger.warning("Snapshot cleanup failed: {}", e)
        for deleted_path in e.deleted:
            _emit_snapshot_deleted(state, snapshot, deleted_path, success=True)
        _emit_snapshot_deleted(
            state, snapshot, e.failed_target, success=False, error_message=str(e)
        )
        return
    except SnapshotError as e:
        logger.warning("Snapshot cleanup failed: {}", e)
        _emit_snapshot_deleted(
            state,
            snapshot,
            snapshot.snapshot_path,
            success=False,
            error_message=str(e),
        )
        return
    for deleted_path in deleted_paths:
        _emit_snapshot_deleted(state, snapshot, deleted_path, success=True)


def _emit_snapshot_deleted(
    state: _LoopState,
    snapshot: SnapshotResult,
    snapshot_path: str,
    *,
    success: bool,
    error_message: str = "",
) -> None:
    write_event(
        state.events_dir,
        make_event(
            BackupEventType.SNAPSHOT_DELETED,
            tick_id=state.current_tick_id,
            method=snapshot.method.value,
            snapshot_path=snapshot_path,
            success=success,
            error_message=error_message,
        ),
    )


def _run_restic_backup(
    *,
    state: _LoopState,
    config: BackupConfig,
    snapshot: SnapshotResult,
    env_overrides: Mapping[str, str],
    backup_fn: BackupFn = restic_backup,
    unlock_fn: UnlockFn = restic_unlock,
) -> bool:
    """Run `restic backup` against the snapshot; emit success or failure event.

    A backup blocked by a stale repository lock (a lock left by a dead PID from a
    prior container incarnation) is recovered from automatically: `restic unlock`
    clears the stale lock and the backup is retried once. Repeated failures bump
    a consecutive-failure counter and, past a threshold, raise a durable alarm so
    an outage cannot pass silently. `backup_fn`/`unlock_fn` are injected for tests.
    """
    tag = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    result, duration = _attempt_backup_with_unlock_retry(
        snapshot=snapshot,
        excludes=config.excludes,
        tag=tag,
        env_overrides=env_overrides,
        backup_fn=backup_fn,
        unlock_fn=unlock_fn,
    )
    if result.returncode != 0:
        state.consecutive_backup_failures += 1
        write_event(
            state.events_dir,
            make_event(
                BackupEventType.RESTIC_BACKUP_FAILED,
                tick_id=state.current_tick_id,
                source_path=str(snapshot.read_path),
                exit_code=result.returncode,
                duration_seconds=duration,
                stdout=result.stdout,
                stderr=result.stderr,
                consecutive_failures=state.consecutive_backup_failures,
            ),
        )
        logger.error(
            "restic backup failed (rc={}): {}", result.returncode, result.stderr.strip()
        )
        _maybe_emit_repeated_failure_alarm(state)
        return False
    state.consecutive_backup_failures = 0
    snapshot_id = extract_snapshot_id_from_backup_output(result.stdout)
    write_event(
        state.events_dir,
        make_event(
            BackupEventType.RESTIC_BACKUP_SUCCEEDED,
            tick_id=state.current_tick_id,
            snapshot_id=snapshot_id,
            source_path=str(snapshot.read_path),
            duration_seconds=duration,
            stdout=result.stdout,
            stderr=result.stderr,
        ),
    )
    return True


def _attempt_backup_with_unlock_retry(
    *,
    snapshot: SnapshotResult,
    excludes: tuple[str, ...],
    tag: str,
    env_overrides: Mapping[str, str],
    backup_fn: BackupFn,
    unlock_fn: UnlockFn,
) -> tuple[subprocess.CompletedProcess[str], float]:
    """Run the backup, clearing a stale lock and retrying once if one blocks it.

    Returns (final restic result, total elapsed seconds). Only a lock error
    triggers the unlock; any other failure is returned as-is for the caller to
    record.
    """
    start = time.monotonic()
    result = backup_fn(
        source_path=snapshot.read_path,
        excludes=excludes,
        tag=tag,
        env_overrides=env_overrides,
    )
    if result.returncode != 0 and is_repo_locked_error(result.stderr):
        logger.warning(
            "restic backup blocked by an existing repository lock; running "
            "`restic unlock` to clear stale locks and retrying once"
        )
        unlock_result = unlock_fn(env_overrides=env_overrides)
        if unlock_result.returncode != 0:
            logger.warning(
                "restic unlock failed (rc={}): {}",
                unlock_result.returncode,
                unlock_result.stderr.strip(),
            )
        else:
            result = backup_fn(
                source_path=snapshot.read_path,
                excludes=excludes,
                tag=tag,
                env_overrides=env_overrides,
            )
    duration = time.monotonic() - start
    return result, duration


def _maybe_emit_repeated_failure_alarm(state: _LoopState) -> None:
    """Raise a durable, prominent alarm once backups have failed N ticks running."""
    if state.consecutive_backup_failures < CONSECUTIVE_FAILURE_ALARM_THRESHOLD:
        return
    logger.error(
        "host_backup has failed {} consecutive ticks; no successful backup is "
        "being taken -- investigate the restic.env / repository",
        state.consecutive_backup_failures,
    )
    write_event(
        state.events_dir,
        make_event(
            BackupEventType.BACKUP_REPEATEDLY_FAILING,
            tick_id=state.current_tick_id,
            consecutive_failures=state.consecutive_backup_failures,
            threshold=CONSECUTIVE_FAILURE_ALARM_THRESHOLD,
        ),
    )


def _run_forget(
    *,
    state: _LoopState,
    config: BackupConfig,
    env_overrides: Mapping[str, str],
) -> None:
    """Run `restic forget` (no prune); always emit FORGET_COMPLETED."""
    start = time.monotonic()
    result = restic_forget(
        keep_hourly=config.retention.keep_hourly,
        keep_daily=config.retention.keep_daily,
        keep_weekly=config.retention.keep_weekly,
        keep_monthly=config.retention.keep_monthly,
        env_overrides=env_overrides,
    )
    duration = time.monotonic() - start
    write_event(
        state.events_dir,
        make_event(
            BackupEventType.FORGET_COMPLETED,
            tick_id=state.current_tick_id,
            exit_code=result.returncode,
            duration_seconds=duration,
            stdout=result.stdout,
            stderr=result.stderr,
        ),
    )
    if result.returncode != 0:
        logger.warning(
            "restic forget failed (rc={}): {}", result.returncode, result.stderr.strip()
        )


def _maybe_run_prune(
    *,
    state: _LoopState,
    config: BackupConfig,
    env_overrides: Mapping[str, str],
) -> None:
    """Run `restic prune` iff the gate file is older than prune_interval_hours."""
    interval_seconds = config.retention.prune_interval_hours * 3600.0
    last_prune = _safe_mtime(PRUNE_TIMESTAMP_PATH)
    now = time.time()
    if last_prune is not None:
        age_seconds = now - last_prune
        if age_seconds < interval_seconds:
            write_event(
                state.events_dir,
                make_event(
                    BackupEventType.PRUNE_SKIPPED,
                    tick_id=state.current_tick_id,
                    age_hours=age_seconds / 3600.0,
                    interval_hours=config.retention.prune_interval_hours,
                ),
            )
            return
    start = time.monotonic()
    result = restic_prune(env_overrides)
    duration = time.monotonic() - start
    write_event(
        state.events_dir,
        make_event(
            BackupEventType.PRUNE_COMPLETED,
            tick_id=state.current_tick_id,
            exit_code=result.returncode,
            duration_seconds=duration,
            stdout=result.stdout,
            stderr=result.stderr,
        ),
    )
    if result.returncode == 0:
        _touch_prune_timestamp()
    else:
        logger.warning(
            "restic prune failed (rc={}): {}", result.returncode, result.stderr.strip()
        )


def _touch_prune_timestamp() -> None:
    """Update PRUNE_TIMESTAMP_PATH to mark the prune as completed."""
    try:
        PRUNE_TIMESTAMP_PATH.parent.mkdir(parents=True, exist_ok=True)
        PRUNE_TIMESTAMP_PATH.write_text(datetime.now(timezone.utc).isoformat())
    except OSError as e:
        logger.warning(
            "Could not update prune timestamp at {}: {}", PRUNE_TIMESTAMP_PATH, e
        )


def _emit_tick_error(state: _LoopState, e: Exception) -> None:
    """Write a TICK_ERROR event capturing the exception type + traceback."""
    write_event(
        state.events_dir,
        make_event(
            BackupEventType.TICK_ERROR,
            tick_id=state.current_tick_id,
            error_type=type(e).__name__,
            error_message=str(e),
            traceback="".join(traceback.format_exception(type(e), e, e.__traceback__)),
        ),
    )


def _max_safe_gap(state: _LoopState) -> float:
    """Floor sleep used by the outermost recovery handler to prevent crash-loop CPU pinning."""
    config = state.last_known_config
    if config is None:
        return 15.0
    return max(config.minimum_backup_gap_seconds, config.config_poll_interval_seconds)


def _safe_mtime(path: Path) -> float | None:
    """Return path.stat().st_mtime, or None if the file is absent or unreadable."""
    try:
        return path.stat().st_mtime
    except OSError:
        return None
