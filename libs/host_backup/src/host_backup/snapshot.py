"""Snapshot mechanisms: btrfs_local, outer_trigger, direct.

Each mechanism implements the same `SnapshotTakerInterface` contract:

- `take_snapshot()` produces a consistent view of the host_dir at a known
  in-container path; returns a `SnapshotResult` describing where restic
  should read.
- `cleanup_after_backup()` reclaims snapshots after restic has read them and
  returns the snapshot paths it deleted (for event logging). For
  `outer_trigger` this retains the newest `max_local_snapshots` and deletes
  the rest; for `btrfs_local` it deletes the single `current` snapshot; for
  `direct` it is a no-op.

The three concrete implementations are selected from `SnapshotSettings.method`.
"""

import json
import subprocess
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Final
from uuid import uuid4

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from loguru import logger
from pydantic import Field

from host_backup.config import SnapshotMethod, SnapshotSettings


class SnapshotError(RuntimeError):
    """Raised when a snapshot step fails (caught by the tick loop)."""


class SnapshotCleanupError(SnapshotError):
    """Raised when keep-N cleanup fails partway, carrying what was already deleted.

    Lets the runner log the deletions that did succeed (and which target failed)
    instead of losing that detail when the exception propagates.
    """

    def __init__(
        self, message: str, *, deleted: tuple[str, ...], failed_target: str
    ) -> None:
        super().__init__(message)
        self.deleted = deleted
        self.failed_target = failed_target


class SnapshotResult(FrozenModel):
    """Outcome of a successful `take_snapshot` call."""

    method: SnapshotMethod = Field(description="Which mechanism produced this snapshot")
    snapshot_path: str = Field(
        description="Outer-side path of the snapshot (for logging / debugging)"
    )
    read_path: Path = Field(description="In-container path restic should read from")
    duration_seconds: float = Field(description="Wall-clock time take_snapshot took")
    helper_exit_code: int | None = Field(
        default=None,
        description="Exit code from the outer helper (outer_trigger only)",
    )
    helper_stdout: str = Field(default="")
    helper_stderr: str = Field(default="")


class SnapshotTakerInterface(MutableModel, ABC):
    """Strategy interface for the three snapshot mechanisms."""

    settings: SnapshotSettings = Field(
        frozen=True, description="Resolved snapshot config"
    )

    @abstractmethod
    def take_snapshot(self) -> SnapshotResult:
        """Produce a consistent snapshot; raises SnapshotError on failure."""

    @abstractmethod
    def cleanup_after_backup(self) -> tuple[str, ...]:
        """Reclaim snapshots after restic has read them; return deleted paths."""


def make_snapshot_taker(settings: SnapshotSettings) -> SnapshotTakerInterface:
    """Build the right SnapshotTakerInterface implementation for `settings.method`."""
    match settings.method:
        case SnapshotMethod.BTRFS_LOCAL:
            _require_paths(settings, ("host_subvolume_path", "snapshot_current_path"))
            return BtrfsLocalSnapshotTaker(settings=settings)
        case SnapshotMethod.OUTER_TRIGGER:
            _require_paths(
                settings,
                (
                    "host_subvolume_path",
                    "snapshot_current_path",
                    "snapshot_read_path",
                    "trigger_dir",
                ),
            )
            return OuterTriggerSnapshotTaker(settings=settings)
        case SnapshotMethod.DIRECT:
            return DirectSnapshotTaker(settings=settings)


def _require_paths(settings: SnapshotSettings, field_names: tuple[str, ...]) -> None:
    """Raise SnapshotError if any of `field_names` is None on `settings`."""
    missing = [name for name in field_names if getattr(settings, name) is None]
    if missing:
        raise SnapshotError(
            f"snapshot.method={settings.method.value} requires fields {missing} in backup.toml"
        )


# ---------------------------------------------------------------------------
# btrfs_local: lima case -- the script itself runs btrfs commands via sudo.
# ---------------------------------------------------------------------------


_BTRFS_TIMEOUT_SECONDS: Final[float] = 60.0


class BtrfsLocalSnapshotTaker(SnapshotTakerInterface):
    """Directly invokes `sudo btrfs subvolume snapshot/delete` (in-VM)."""

    def take_snapshot(self) -> SnapshotResult:
        # Delete any leftover snapshot first; tolerant of "doesn't exist".
        self.delete_snapshot()
        start = time.monotonic()
        source = self.settings.host_subvolume_path
        target = self.settings.snapshot_current_path
        assert (
            source is not None and target is not None
        )  # checked by make_snapshot_taker
        target.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["sudo", "btrfs", "subvolume", "snapshot", "-r", str(source), str(target)],
            capture_output=True,
            text=True,
            check=False,
            timeout=_BTRFS_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            raise SnapshotError(
                f"btrfs subvolume snapshot failed (rc={result.returncode}): "
                f"stderr={result.stderr.strip()!r}"
            )
        duration = time.monotonic() - start
        read_path = self.settings.snapshot_read_path or target
        return SnapshotResult(
            method=SnapshotMethod.BTRFS_LOCAL,
            snapshot_path=str(target),
            read_path=read_path,
            duration_seconds=duration,
            helper_stdout=result.stdout,
            helper_stderr=result.stderr,
        )

    def cleanup_after_backup(self) -> tuple[str, ...]:
        # lima keeps a single `current` snapshot; delete it after each backup
        # exactly as before.
        target = self.settings.snapshot_current_path
        existed = target is not None and target.exists()
        self.delete_snapshot()
        return (str(target),) if existed else ()

    def delete_snapshot(self) -> None:
        target = self.settings.snapshot_current_path
        if target is None:
            return
        if not target.exists():
            return
        result = subprocess.run(
            ["sudo", "btrfs", "subvolume", "delete", str(target)],
            capture_output=True,
            text=True,
            check=False,
            timeout=_BTRFS_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            raise SnapshotError(
                f"btrfs subvolume delete failed (rc={result.returncode}): "
                f"stderr={result.stderr.strip()!r}"
            )


# ---------------------------------------------------------------------------
# outer_trigger: vps-docker case -- RPC to a systemd unit on the outer host.
# ---------------------------------------------------------------------------


_HELPER_POLL_INTERVAL_SECONDS: Final[float] = 1.0


class _HelperResult(FrozenModel):
    """Parsed contents of the outer helper's result.json."""

    request_id: str
    operation: str
    exit_code: int
    stdout: str
    stderr: str
    snapshot_path: str = ""


class OuterTriggerSnapshotTaker(SnapshotTakerInterface):
    """Writes request.json, waits for the outer helper to produce result.json."""

    def take_snapshot(self) -> SnapshotResult:
        # Create a fresh, uniquely-named snapshot. We never reuse a path: under
        # gVisor the gofer caches a handle to the first subvolume it opens at a
        # given path, so deleting and recreating one path makes every snapshot
        # after the first read empty. The request id (a timestamp) doubles as
        # the snapshot's directory name, and cleanup_after_backup garbage-
        # collects old snapshots by name.
        assert self.settings.snapshot_read_path is not None
        assert self.settings.snapshot_current_path is not None
        start = time.monotonic()
        snapshot_name = _iso_now()
        result = self._do_request("snapshot", request_id=snapshot_name)
        duration = time.monotonic() - start
        if result.exit_code != 0:
            raise SnapshotError(
                f"outer helper snapshot failed (rc={result.exit_code}): "
                f"stderr={result.stderr.strip()!r}"
            )
        outer_snapshot_path = result.snapshot_path or str(
            self.settings.snapshot_current_path.parent / snapshot_name
        )
        return SnapshotResult(
            method=SnapshotMethod.OUTER_TRIGGER,
            snapshot_path=outer_snapshot_path,
            read_path=self.settings.snapshot_read_path.parent / snapshot_name,
            duration_seconds=duration,
            helper_exit_code=result.exit_code,
            helper_stdout=result.stdout,
            helper_stderr=result.stderr,
        )

    def cleanup_after_backup(self) -> tuple[str, ...]:
        # Retain the newest `max_local_snapshots`; delete the rest by name. The
        # parent of the configured read/current path is the snapshots dir (the
        # `current` basename in the config is vestigial under the per-name
        # scheme). We enumerate over the read mount -- listing the parent dir is
        # reliable; only same-path inode swaps were affected by the gofer bug.
        assert self.settings.snapshot_read_path is not None
        assert self.settings.snapshot_current_path is not None
        read_dir = self.settings.snapshot_read_path.parent
        outer_dir = self.settings.snapshot_current_path.parent
        names = _list_snapshot_names(read_dir)
        surplus_count = max(0, len(names) - self.settings.max_local_snapshots)
        deleted: list[str] = []
        for name in names[:surplus_count]:
            result = self._do_request("cleanup", request_id=uuid4().hex, target=name)
            if result.exit_code != 0:
                raise SnapshotCleanupError(
                    f"outer helper cleanup failed (rc={result.exit_code}): "
                    f"stderr={result.stderr.strip()!r}",
                    deleted=tuple(deleted),
                    failed_target=str(outer_dir / name),
                )
            deleted.append(str(outer_dir / name))
        return tuple(deleted)

    def _do_request(
        self, operation: str, request_id: str, target: str | None = None
    ) -> _HelperResult:
        """Send a request.json to the outer helper and wait for its matching result.json."""
        trigger_dir = self.settings.trigger_dir
        assert trigger_dir is not None
        trigger_dir.mkdir(parents=True, exist_ok=True)
        request_payload: dict[str, str] = {
            "request_id": request_id,
            "operation": operation,
            "timestamp_iso": _iso_now(),
        }
        if target is not None:
            request_payload["target"] = target
        request_path = trigger_dir / "request.json"
        result_path = trigger_dir / "result.json"

        tmp_path = trigger_dir / "request.json.tmp"
        tmp_path.write_text(json.dumps(request_payload))
        tmp_path.replace(request_path)
        logger.debug(
            "Sent outer-helper request id={} op={} via {}",
            request_id,
            operation,
            request_path,
        )

        # The request_id is unique per request (a microsecond timestamp for
        # snapshots, a uuid for cleanups), so result.json carrying our id
        # unambiguously means the helper serviced *this* request -- we key on that
        # rather than on a result.json mtime change. The old mtime gate could both
        # miss a same-mtime rewrite (coarse-resolution filesystems) and accept a
        # stale result from a prior request whose id happened to be re-read; the
        # id match is the authoritative, race-free freshness signal.
        deadline = time.monotonic() + self.settings.outer_helper_timeout_seconds
        while True:
            parsed = _parse_helper_result(result_path)
            if parsed is not None and parsed.request_id == request_id:
                return parsed
            # Absent, unparseable, or a result for a different request: keep polling.
            if time.monotonic() >= deadline:
                raise SnapshotError(
                    f"Timed out after {self.settings.outer_helper_timeout_seconds}s "
                    f"waiting for outer helper result for request_id={request_id}"
                )
            time.sleep(_HELPER_POLL_INTERVAL_SECONDS)


def _parse_helper_result(path: Path) -> _HelperResult | None:
    """Load and validate result.json; returns None on parse error so caller can keep polling."""
    try:
        text = path.read_text()
    except OSError:
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return _HelperResult.model_validate(payload)
    except ValueError:
        return None


_SNAPSHOT_NAME_FORMAT: Final[str] = "%Y-%m-%dT%H:%M:%S.%fZ"


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime(_SNAPSHOT_NAME_FORMAT)


def _parse_snapshot_timestamp(name: str) -> datetime | None:
    """Parse a snapshot dir name back to its timestamp, or None if it isn't one."""
    try:
        return datetime.strptime(name, _SNAPSHOT_NAME_FORMAT)
    except ValueError:
        return None


def _list_snapshot_names(read_dir: Path) -> list[str]:
    """Return timestamped snapshot dir names under `read_dir`, oldest first.

    Entries whose names don't parse as a snapshot timestamp are ignored, so a
    stray or partial directory can never be selected for deletion.
    """
    try:
        entries = list(read_dir.iterdir())
    except OSError:
        return []
    timestamped = [
        (timestamp, entry.name)
        for entry in entries
        if (timestamp := _parse_snapshot_timestamp(entry.name)) is not None
    ]
    timestamped.sort(key=lambda pair: pair[0])
    return [name for _, name in timestamped]


# ---------------------------------------------------------------------------
# direct: plain docker (no btrfs) -- restic reads /mngr/ live.
# ---------------------------------------------------------------------------


class DirectSnapshotTaker(SnapshotTakerInterface):
    """No-op snapshot; restic reads the host_dir live (used in plain docker dev/test)."""

    def take_snapshot(self) -> SnapshotResult:
        read_path = self.settings.snapshot_read_path or Path("/mngr")
        return SnapshotResult(
            method=SnapshotMethod.DIRECT,
            snapshot_path=str(read_path),
            read_path=read_path,
            duration_seconds=0.0,
        )

    def cleanup_after_backup(self) -> tuple[str, ...]:
        # Nothing was snapshotted (restic reads /mngr live), so nothing to clean.
        return ()
