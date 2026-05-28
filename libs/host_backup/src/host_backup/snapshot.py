"""Snapshot mechanisms: btrfs_local, outer_trigger, direct.

Each mechanism implements the same `SnapshotTakerInterface` contract:

- `take_snapshot()` produces a consistent view of the host_dir at a known
  in-container path; returns a `SnapshotResult` describing where restic
  should read.
- `delete_snapshot()` removes whatever `take_snapshot` produced. Idempotent.

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
    def delete_snapshot(self) -> None:
        """Tear down the most-recent snapshot. Idempotent (no-op when absent)."""


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
        # Always cleanup first via the helper, then create the new snapshot.
        self.delete_snapshot()
        start = time.monotonic()
        request_id = uuid4().hex
        result = self._do_request("snapshot", request_id)
        duration = time.monotonic() - start
        if result.exit_code != 0:
            raise SnapshotError(
                f"outer helper snapshot failed (rc={result.exit_code}): "
                f"stderr={result.stderr.strip()!r}"
            )
        assert self.settings.snapshot_read_path is not None
        snapshot_path = result.snapshot_path or str(self.settings.snapshot_current_path)
        return SnapshotResult(
            method=SnapshotMethod.OUTER_TRIGGER,
            snapshot_path=snapshot_path,
            read_path=self.settings.snapshot_read_path,
            duration_seconds=duration,
            helper_exit_code=result.exit_code,
            helper_stdout=result.stdout,
            helper_stderr=result.stderr,
        )

    def delete_snapshot(self) -> None:
        request_id = uuid4().hex
        result = self._do_request("cleanup", request_id)
        # The outer helper is expected to treat "snapshot didn't exist" as success,
        # so a non-zero exit really is an error here.
        if result.exit_code != 0:
            raise SnapshotError(
                f"outer helper cleanup failed (rc={result.exit_code}): "
                f"stderr={result.stderr.strip()!r}"
            )

    def _do_request(self, operation: str, request_id: str) -> _HelperResult:
        """Send a request.json to the outer helper and wait for its matching result.json."""
        trigger_dir = self.settings.trigger_dir
        assert trigger_dir is not None
        trigger_dir.mkdir(parents=True, exist_ok=True)
        request_payload = {
            "request_id": request_id,
            "operation": operation,
            "timestamp_iso": _iso_now(),
        }
        request_path = trigger_dir / "request.json"
        result_path = trigger_dir / "result.json"
        previous_result_mtime = _safe_mtime(result_path)

        tmp_path = trigger_dir / "request.json.tmp"
        tmp_path.write_text(json.dumps(request_payload))
        tmp_path.replace(request_path)
        logger.debug(
            "Sent outer-helper request id={} op={} via {}",
            request_id,
            operation,
            request_path,
        )

        deadline = time.monotonic() + self.settings.outer_helper_timeout_seconds
        while True:
            current_mtime = _safe_mtime(result_path)
            if current_mtime is not None and current_mtime != previous_result_mtime:
                parsed = _parse_helper_result(result_path)
                if parsed is not None and parsed.request_id == request_id:
                    return parsed
                # Stale or unparseable result; keep polling.
            if time.monotonic() >= deadline:
                raise SnapshotError(
                    f"Timed out after {self.settings.outer_helper_timeout_seconds}s "
                    f"waiting for outer helper result for request_id={request_id}"
                )
            time.sleep(_HELPER_POLL_INTERVAL_SECONDS)


def _safe_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


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


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


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

    def delete_snapshot(self) -> None:
        return None
