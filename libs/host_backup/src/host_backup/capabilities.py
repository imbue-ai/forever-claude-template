"""Backup capabilities: what snapshot primitives this environment provides.

Capabilities are environment-derived facts, not configuration: the service
detects them in memory at startup by probing the container's filesystem, and
they are deliberately excluded from `runtime/backup.toml` (which holds only
user settings) and from the minds "backup service up to date" comparison
surface. They are logged and included in the backup events stream for
observability; there is no persisted capabilities file and no override
mechanism.

Detection decision tree (everything is probeable from inside the container):
  - If the trigger dir (`/mngr-snapshot/`) exists as a directory, we are
    inside a vps-docker agent container with the snapshot-trigger volume
    mounted -> `outer_trigger`.
  - Else if the host_dir (`/mngr`) is on a btrfs filesystem (lima), we can
    take snapshots directly via `sudo btrfs subvolume snapshot`
    -> `btrfs_local`.
  - Else (plain docker / any unrecognized provider) -> `direct` (no
    snapshot; restic reads the host_dir live).
"""

import subprocess
from enum import auto
from pathlib import Path
from typing import Final

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from loguru import logger
from pydantic import Field

# Well-known in-container paths probed by detection. The outer helper resolves
# the real outer-side subvolume path at request time, so the inner service
# never needs outer-side knowledge beyond these constants.
DEFAULT_TRIGGER_DIR: Final[Path] = Path("/mngr-snapshot")
DEFAULT_HOST_DIR: Final[Path] = Path("/mngr")

_FINDMNT_TIMEOUT_SECONDS: Final[float] = 15.0


class SnapshotMethod(UpperCaseStrEnum):
    """How the service obtains a consistent view of the host_dir before restic reads it."""

    BTRFS_LOCAL = auto()
    OUTER_TRIGGER = auto()
    DIRECT = auto()


class BackupCapabilities(FrozenModel):
    """The snapshot primitives available in this environment, detected at startup."""

    method: SnapshotMethod = Field(description="Snapshot mechanism for this provider")
    btrfs_mount_path: Path | None = Field(
        default=None,
        description=(
            "Outer-side btrfs mount root, e.g. /mngr-btrfs. Used by "
            "outer_trigger to construct the snapshot_path; ignored for direct. "
            "For btrfs_local, set to the in-VM btrfs mount path."
        ),
    )
    host_subvolume_path: Path | None = Field(
        default=None,
        description=(
            "Absolute path of the host's btrfs subvolume on the (outer or in-VM) "
            "btrfs filesystem. The outer helper resolves the real path at request "
            "time for outer_trigger, so the placeholder value is never used there."
        ),
    )
    snapshot_current_path: Path | None = Field(
        default=None,
        description=(
            "Where the live snapshot slot is created on the btrfs filesystem "
            "(the outer's perspective for outer_trigger, the in-VM view for "
            "btrfs_local). For outer_trigger only the PARENT directory is used "
            "(snapshots get unique per-tick names under it)."
        ),
    )
    snapshot_read_path: Path | None = Field(
        default=None,
        description=(
            "Path the in-container restic actually reads from. For outer_trigger "
            "this is /mngr-snapshots/current (the bind mount of the outer's "
            "snapshot dir; only the PARENT directory is used); for btrfs_local it "
            "equals snapshot_current_path; for direct it is the host_dir itself."
        ),
    )
    trigger_dir: Path | None = Field(
        default=None,
        description=(
            "Inner-container dir where request.json / result.json live for "
            "outer_trigger (e.g. /mngr-snapshot). Present only for outer_trigger."
        ),
    )
    outer_helper_timeout_seconds: float = Field(
        default=120.0,
        description="Hard cap on how long to wait for the outer helper's result.json",
    )
    max_local_snapshots: int = Field(
        default=5,
        ge=1,
        description=(
            "outer_trigger only: how many on-host btrfs snapshots to retain. "
            "Each tick creates a new timestamped snapshot and deletes the "
            "oldest beyond this count. Ignored by btrfs_local and direct."
        ),
    )


def detect_backup_capabilities(
    *,
    trigger_dir: Path = DEFAULT_TRIGGER_DIR,
    host_dir: Path = DEFAULT_HOST_DIR,
) -> BackupCapabilities:
    """Probe the container's filesystem to choose the right snapshot mechanism."""
    if trigger_dir.is_dir():
        # vps-docker: snapshots dir is bind-mounted at /mngr-snapshots; the
        # outer helper resolves <btrfs-mount>/<host_id_hex>/snapshots/<name>
        # at request time, so the inner service doesn't need to know the
        # outer-side path -- it just reads what appears at /mngr-snapshots/.
        return BackupCapabilities(
            method=SnapshotMethod.OUTER_TRIGGER,
            btrfs_mount_path=Path("/mngr-btrfs"),
            host_subvolume_path=Path("/mngr-btrfs/<host_id_hex>"),
            snapshot_current_path=Path("/mngr-btrfs/snapshots/current"),
            snapshot_read_path=Path("/mngr-snapshots/current"),
            trigger_dir=trigger_dir,
        )
    fstype = _findmnt_fstype(host_dir)
    if fstype == "btrfs":
        # lima attaches a btrfs additional disk and symlinks host_dir to its
        # mount point, so the btrfs filesystem *is* host_dir. The snapshot must
        # live on that same btrfs (you cannot snapshot a subvolume onto another
        # filesystem), so derive every path from host_dir.
        return BackupCapabilities(
            method=SnapshotMethod.BTRFS_LOCAL,
            btrfs_mount_path=host_dir,
            host_subvolume_path=host_dir,
            snapshot_current_path=host_dir / "snapshots" / "current",
            snapshot_read_path=host_dir / "snapshots" / "current",
        )
    return BackupCapabilities(
        method=SnapshotMethod.DIRECT,
        snapshot_read_path=host_dir,
    )


def _findmnt_fstype(path: Path) -> str:
    """Return the filesystem type for `path` via `findmnt`; empty string on any failure."""
    try:
        result = subprocess.run(
            ["findmnt", "-n", "-o", "FSTYPE", str(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=_FINDMNT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        # An unexpected findmnt failure downgrades detection to `direct` (no
        # snapshots), so surface why rather than falling back silently.
        logger.warning("findmnt failed for {}: {}", path, e)
        return ""
    if result.returncode != 0:
        # Same rationale as above: a nonzero exit silently downgrades a
        # snapshot-capable host to `direct`, so say why.
        logger.warning(
            "findmnt exited {} for {}: {}",
            result.returncode,
            path,
            result.stderr.strip(),
        )
        return ""
    return result.stdout.strip()
