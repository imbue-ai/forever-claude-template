"""`host-backup-now` CLI.

Convenience wrapper for forcing an immediate backup tick. The script's
mtime-polling loop already reacts to config-file edits, so all this CLI
does is touch backup.toml -- except when a backup is currently running,
in which case it waits for the in-flight one to finish first (so the
user's most recent edits are guaranteed to land in the next tick rather
than the in-flight one).
"""

import json
import os
import sys
import time
from pathlib import Path

import click
from loguru import logger

from host_backup.config import BACKUP_TOML_PATH, get_events_dir
from host_backup.events import BACKUP_EVENT_SOURCE, BackupEventType

DEFAULT_TIMEOUT_SECONDS = 1800.0  # 30 minutes
_POLL_INTERVAL_SECONDS = 0.5


@click.command()
@click.option(
    "--timeout",
    "timeout_seconds",
    default=DEFAULT_TIMEOUT_SECONDS,
    show_default=True,
    help="How long (seconds) to wait for the triggered backup to finish",
)
def backup_now_main(timeout_seconds: float) -> None:
    """Trigger an immediate host_backup tick and wait for it to complete."""
    events_dir = get_events_dir()
    if events_dir is None:
        logger.error(
            "MNGR_AGENT_STATE_DIR is not set; cannot tail the backup event log"
        )
        sys.exit(2)
    events_path = events_dir / "events.jsonl"

    deadline = time.monotonic() + timeout_seconds
    initial_size = _safe_file_size(events_path)

    _wait_for_no_inflight_backup(events_path, initial_size, deadline)
    _bump_config_mtime()
    completion = _wait_for_next_completion(
        events_path, _safe_file_size(events_path), deadline
    )
    if completion is None:
        logger.error("Timed out waiting for backup to complete")
        sys.exit(2)
    click.echo(json.dumps(completion, default=str))
    sys.exit(
        0
        if completion.get("type") == BackupEventType.RESTIC_BACKUP_SUCCEEDED.value
        else 1
    )


def _safe_file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _bump_config_mtime() -> None:
    """Update backup.toml's mtime so the runner's poll loop kicks off a new tick."""
    if not BACKUP_TOML_PATH.exists():
        # No config yet; touching anyway so bootstrap can pick it up later
        # is harmless. Create an empty file rather than skipping.
        BACKUP_TOML_PATH.parent.mkdir(parents=True, exist_ok=True)
        BACKUP_TOML_PATH.touch()
        return
    now = time.time()
    os.utime(BACKUP_TOML_PATH, (now, now))


def _wait_for_no_inflight_backup(
    events_path: Path,
    initial_size: int,
    deadline: float,
) -> None:
    """Block until any in-flight backup has emitted a completion event.

    Reads the existing tail of the events file (before bumping mtime) to
    decide whether a backup is currently in flight, by walking events in
    reverse chronological order until we find either a started-without-
    completion (in flight) or a completion (we're idle).
    """
    pending_tick_ids = _scan_for_inflight_tick_ids(events_path, max_lines=200)
    if not pending_tick_ids:
        return
    logger.info(
        "Waiting for {} in-flight backup tick(s) to complete...", len(pending_tick_ids)
    )
    last_size = initial_size
    while pending_tick_ids:
        if time.monotonic() >= deadline:
            return
        new_size = _safe_file_size(events_path)
        if new_size > last_size:
            for event in _read_new_events(events_path, last_size, new_size):
                tick_id = event.get("tick_id")
                if isinstance(tick_id, str) and event.get("type") in (
                    BackupEventType.RESTIC_BACKUP_SUCCEEDED.value,
                    BackupEventType.RESTIC_BACKUP_FAILED.value,
                    BackupEventType.TICK_SKIPPED_DUE_TO_MISSING_SECRETS.value,
                    BackupEventType.TICK_ERROR.value,
                ):
                    pending_tick_ids.discard(tick_id)
            last_size = new_size
        time.sleep(_POLL_INTERVAL_SECONDS)


def _wait_for_next_completion(
    events_path: Path,
    initial_size: int,
    deadline: float,
) -> dict[str, object] | None:
    """Block until the next RESTIC_BACKUP_SUCCEEDED / FAILED event appears."""
    last_size = initial_size
    while time.monotonic() < deadline:
        new_size = _safe_file_size(events_path)
        if new_size > last_size:
            for event in _read_new_events(events_path, last_size, new_size):
                if event.get("type") in (
                    BackupEventType.RESTIC_BACKUP_SUCCEEDED.value,
                    BackupEventType.RESTIC_BACKUP_FAILED.value,
                ):
                    return event
            last_size = new_size
        time.sleep(_POLL_INTERVAL_SECONDS)
    return None


def _scan_for_inflight_tick_ids(events_path: Path, *, max_lines: int) -> set[str]:
    """Look at the last `max_lines` events; return the set of tick_ids that started but did not finish."""
    if not events_path.exists():
        return set()
    try:
        lines = events_path.read_text().splitlines()
    except OSError:
        return set()
    started: set[str] = set()
    finished: set[str] = set()
    for raw in lines[-max_lines:]:
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("source") != BACKUP_EVENT_SOURCE:
            continue
        tick_id = event.get("tick_id")
        if not isinstance(tick_id, str):
            continue
        event_type = event.get("type")
        if event_type == BackupEventType.BACKUP_STARTED.value:
            started.add(tick_id)
        elif event_type in (
            BackupEventType.RESTIC_BACKUP_SUCCEEDED.value,
            BackupEventType.RESTIC_BACKUP_FAILED.value,
            BackupEventType.TICK_SKIPPED_DUE_TO_MISSING_SECRETS.value,
            BackupEventType.TICK_ERROR.value,
        ):
            finished.add(tick_id)
    return started - finished


def _read_new_events(
    events_path: Path, last_size: int, new_size: int
) -> list[dict[str, object]]:
    """Read events appended between byte offsets last_size and new_size."""
    try:
        with events_path.open("rb") as fh:
            fh.seek(last_size)
            blob = fh.read(new_size - last_size)
    except OSError:
        return []
    events: list[dict[str, object]] = []
    for line in blob.decode(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


if __name__ == "__main__":
    backup_now_main()
