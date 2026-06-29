"""Structured event types + writer for the host_backup service.

Events land at `$MNGR_AGENT_STATE_DIR/events/backup/events.jsonl`, one
JSONL line per event, with the standard envelope (timestamp, type,
event_id, source) plus event-specific fields. The full stdout/stderr of
each restic command is embedded in the matching event so operators can
diagnose failures without rerunning anything.
"""

import json
from datetime import datetime, timezone
from enum import auto
from pathlib import Path
from typing import Final
from uuid import uuid4

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.event_envelope import (
    EventEnvelope,
    EventId,
    EventSource,
    EventType,
    IsoTimestamp,
)
from loguru import logger
from pydantic import Field

BACKUP_EVENT_SOURCE: Final[EventSource] = EventSource("backup")


class BackupEventType(UpperCaseStrEnum):
    """All event types the host_backup service may emit."""

    BACKUP_STARTED = auto()
    SNAPSHOT_CREATED = auto()
    SNAPSHOT_FAILED = auto()
    SNAPSHOT_DELETED = auto()
    RESTIC_BACKUP_SUCCEEDED = auto()
    RESTIC_BACKUP_FAILED = auto()
    FORGET_COMPLETED = auto()
    PRUNE_COMPLETED = auto()
    PRUNE_SKIPPED = auto()
    CONFIG_RELOADED = auto()
    REPO_INIT_ATTEMPTED = auto()
    REPO_INIT_SUCCEEDED = auto()
    TICK_SKIPPED_DUE_TO_MISSING_SECRETS = auto()
    TICK_ERROR = auto()


class BackupEvent(EventEnvelope):
    """Base envelope for every host_backup event; subclasses add payload fields."""


class BackupStartedEvent(BackupEvent):
    """A new backup tick has begun."""

    tick_id: str = Field(
        description="Per-tick uuid; correlates with the completion event"
    )
    trigger_reason: str = Field(
        description="Why this tick fired: interval | config_change | startup"
    )


class SnapshotCreatedEvent(BackupEvent):
    """A consistent snapshot of /mngr/ is in place for restic to read."""

    tick_id: str
    method: str = Field(description="btrfs_local | outer_trigger | direct")
    snapshot_path: str = Field(
        description="Where the snapshot's `current/` slot ended up"
    )
    duration_seconds: float
    helper_exit_code: int | None = Field(
        default=None,
        description="Exit code from the outer helper (outer_trigger only)",
    )
    helper_stdout: str = Field(default="")
    helper_stderr: str = Field(default="")


class SnapshotFailedEvent(BackupEvent):
    """The snapshot step failed; the tick was aborted before restic ran."""

    tick_id: str
    method: str = Field(description="btrfs_local | outer_trigger | direct")
    error_message: str = Field(
        description="Failure detail (for outer_trigger, includes the helper's exit code + stderr)"
    )


class SnapshotDeletedEvent(BackupEvent):
    """The post-backup `current/` snapshot has been cleaned up."""

    tick_id: str
    method: str
    snapshot_path: str
    success: bool
    error_message: str = Field(default="")


class ResticBackupSucceededEvent(BackupEvent):
    """`restic backup` returned 0."""

    tick_id: str
    snapshot_id: str = Field(
        default="", description="Restic snapshot id from --json output"
    )
    source_path: str
    duration_seconds: float
    stdout: str
    stderr: str


class ResticBackupFailedEvent(BackupEvent):
    """`restic backup` returned non-zero."""

    tick_id: str
    source_path: str
    exit_code: int
    duration_seconds: float
    stdout: str
    stderr: str


class ForgetCompletedEvent(BackupEvent):
    """`restic forget` finished (index update, no data deletion)."""

    tick_id: str
    exit_code: int
    duration_seconds: float
    stdout: str
    stderr: str


class PruneCompletedEvent(BackupEvent):
    """`restic prune` finished (actual data deletion)."""

    tick_id: str
    exit_code: int
    duration_seconds: float
    stdout: str
    stderr: str


class PruneSkippedEvent(BackupEvent):
    """`restic prune` was skipped because the gate file is too recent."""

    tick_id: str
    age_hours: float
    interval_hours: float


class ConfigReloadedEvent(BackupEvent):
    """backup.toml was re-read for this tick."""

    tick_id: str
    backup_toml_mtime: float
    restic_env_mtime: float | None


class RepoInitAttemptedEvent(BackupEvent):
    """A `restic init` attempt was launched."""

    tick_id: str
    repository_url: str


class RepoInitSucceededEvent(BackupEvent):
    """A `restic init` attempt returned 0."""

    tick_id: str
    repository_url: str
    stdout: str
    stderr: str


class TickSkippedDueToMissingSecretsEvent(BackupEvent):
    """The current tick was skipped because restic.env is incomplete."""

    tick_id: str
    missing_keys: tuple[str, ...]


class TickErrorEvent(BackupEvent):
    """An unhandled (or otherwise unexpected) error was caught in the tick loop."""

    tick_id: str
    error_type: str
    error_message: str
    traceback: str


def new_event_id() -> EventId:
    return EventId(f"evt-{uuid4().hex}")


def now_iso() -> IsoTimestamp:
    """Current UTC time as nanosecond-precision ISO-8601 with trailing Z."""
    now = datetime.now(timezone.utc)
    # `%f` is microseconds; pad with `000` to get nanoseconds, per the
    # event_envelope convention.
    return IsoTimestamp(now.strftime("%Y-%m-%dT%H:%M:%S.%f000Z"))


def make_event(event_type: BackupEventType, **fields: object) -> dict[str, object]:
    """Build a fully-populated event dict for the given type with envelope fields filled in.

    Returns a plain dict rather than a pydantic model so callers can pass
    arbitrary extra fields without having to plumb each one through a
    typed subclass; the typed subclasses above exist for documentation +
    test assertions, not as a hard constraint on the writer.
    """
    payload: dict[str, object] = {
        "timestamp": now_iso(),
        "type": EventType(event_type.value),
        "event_id": new_event_id(),
        "source": BACKUP_EVENT_SOURCE,
    }
    payload.update(fields)
    return payload


def write_event(events_dir: Path | None, event: dict[str, object]) -> None:
    """Append `event` as a JSONL line to events_dir/events.jsonl.

    When `events_dir` is None (state dir unset), only logs at warning level
    so the service still runs in test / debugging environments without an
    agent context.
    """
    if events_dir is None:
        logger.warning(
            "MNGR_AGENT_STATE_DIR unset; dropping backup event {}", event.get("type")
        )
        return
    try:
        events_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("Cannot create events dir {}: {}", events_dir, e)
        return
    events_path = events_dir / "events.jsonl"
    try:
        with events_path.open("a") as fh:
            fh.write(json.dumps(event, default=str))
            fh.write("\n")
    except OSError as e:
        logger.warning("Cannot append to {}: {}", events_path, e)
