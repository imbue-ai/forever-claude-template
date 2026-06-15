"""Backup configuration loading and default-template writing.

The config lives in two on-disk files written by `libs/bootstrap` on first
boot:

- `runtime/backup.toml` -- non-secret script settings (interval, retention,
  excludes, snapshot method, and the `allow_empty_password` restic knob).
  Rides the runtime-backup git push.
- `runtime/secrets/restic.env` -- restic's repository address + all secrets
  (`RESTIC_REPOSITORY`, `RESTIC_PASSWORD`, and any backend credentials restic
  reads from the environment, e.g. `AWS_ACCESS_KEY_ID` /
  `AWS_SECRET_ACCESS_KEY` for an S3/R2 backend). Gitignored.

This module defines the frozen `BackupConfig` model and helpers for
loading both files, including the merge logic that bootstrap uses to
preserve user-customized fields when re-detecting the environment.
"""

import os
import tomllib
from enum import auto
from pathlib import Path
from typing import Final

import tomlkit
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from pydantic import Field

BACKUP_TOML_PATH: Final[Path] = Path("runtime/backup.toml")
RESTIC_ENV_PATH: Final[Path] = Path("runtime/secrets/restic.env")
PRUNE_TIMESTAMP_PATH: Final[Path] = Path("runtime/last-restic-prune")


class BackupConfigError(ValueError):
    """Raised when backup.toml cannot be loaded or validated."""


class SnapshotMethod(UpperCaseStrEnum):
    """How the script obtains a consistent view of /mngr/ before restic reads it."""

    BTRFS_LOCAL = auto()
    OUTER_TRIGGER = auto()
    DIRECT = auto()


class SnapshotSettings(FrozenModel):
    """Filesystem paths + protocol used by the snapshot step.

    Bootstrap populates this section by probing the environment; the script
    just reads it.
    """

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
            "btrfs filesystem, e.g. /mngr-btrfs/<host_id_hex>. Required for "
            "btrfs_local and outer_trigger."
        ),
    )
    snapshot_current_path: Path | None = Field(
        default=None,
        description=(
            "Where the live snapshot's `current/` slot is created on the btrfs "
            "filesystem (the outer's perspective for outer_trigger, the in-VM "
            "view for btrfs_local). Required for btrfs_local and outer_trigger. "
            "For outer_trigger only the PARENT directory is used (snapshots get "
            "unique per-tick names under it); the `current` basename is vestigial."
        ),
    )
    snapshot_read_path: Path | None = Field(
        default=None,
        description=(
            "Path the in-container restic actually reads from. For outer_trigger "
            "this is /mngr-snapshots/current (the bind mount of the outer's "
            "snapshot dir); for btrfs_local it equals snapshot_current_path. "
            "For outer_trigger only the PARENT directory is used (the per-tick "
            "snapshot name is appended at runtime); the `current` basename is "
            "vestigial."
        ),
    )
    trigger_dir: Path | None = Field(
        default=None,
        description=(
            "Inner-container dir where request.json / result.json live for "
            "outer_trigger (e.g. /mngr-snapshot). Required only for outer_trigger."
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


class RetentionSettings(FrozenModel):
    """Restic forget retention policy."""

    keep_hourly: int = Field(
        default=24, description="How many hourly snapshots to retain"
    )
    keep_daily: int = Field(
        default=30, description="How many daily snapshots to retain"
    )
    keep_weekly: int = Field(
        default=12, description="How many weekly snapshots to retain"
    )
    keep_monthly: int = Field(
        default=24, description="How many monthly snapshots to retain"
    )
    prune_interval_hours: float = Field(
        default=24.0,
        description="Minimum gap between successive `restic prune` runs",
    )


class BackupConfig(FrozenModel):
    """Top-level backup script configuration loaded from runtime/backup.toml."""

    backup_interval_seconds: float = Field(
        default=3600.0,
        description="Wall-clock interval between backup ticks",
    )
    minimum_backup_gap_seconds: float = Field(
        default=60.0,
        description=(
            "Hard floor on the gap between successive backup attempts (prevents "
            "error-log spam under pathological config-change cycles)"
        ),
    )
    config_poll_interval_seconds: float = Field(
        default=15.0,
        description="Mtime poll interval for backup.toml + restic.env",
    )
    snapshot: SnapshotSettings = Field(description="Snapshot mechanism + paths")
    retention: RetentionSettings = Field(default_factory=RetentionSettings)
    excludes: tuple[str, ...] = Field(
        default=(
            "**/.venv",
            "**/node_modules",
            "**/__pycache__",
            "**/.pytest_cache",
            "**/.ruff_cache",
            "**/target",
            "**/dist",
            "**/build",
            "**/.next",
            "**/.cache",
        ),
        description="Glob patterns passed to `restic backup --exclude=...`",
    )


def load_backup_config(path: Path = BACKUP_TOML_PATH) -> BackupConfig:
    """Load and validate backup.toml; raises BackupConfigError on any failure."""
    if not path.exists():
        raise BackupConfigError(f"Backup config not found at {path}")
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise BackupConfigError(f"Failed to read/parse {path}: {e}") from e
    try:
        return BackupConfig.model_validate(raw)
    except ValueError as e:
        raise BackupConfigError(f"Invalid backup config in {path}: {e}") from e


def parse_restic_env_file(content: str) -> dict[str, str]:
    """Parse a KEY=value env file (supports leading `export `, comments, blanks).

    Quoted values (single or double) are unquoted. No shell expansion is
    performed; this is the same envelope contract as bootstrap's host
    env file parser.
    """
    result: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        result[key] = value
    return result


def load_restic_env(path: Path = RESTIC_ENV_PATH) -> dict[str, str]:
    """Load restic.env into a dict. Returns {} if the file is absent."""
    if not path.exists():
        return {}
    try:
        return parse_restic_env_file(path.read_text())
    except OSError as e:
        raise BackupConfigError(f"Failed to read {path}: {e}") from e


def missing_required_restic_keys(env: dict[str, str]) -> list[str]:
    """Return the restic.env keys that must be present before a backup can run.

    `RESTIC_REPOSITORY` (the only source of the repository address) and
    `RESTIC_PASSWORD` are both required -- minds always provisions a
    per-workspace password, so the repo is never empty-password here. Backend
    credentials (e.g. `AWS_*`) are intentionally not required: which ones are
    needed depends on the `RESTIC_REPOSITORY` backend, so restic itself
    reports a clear error if a required one is missing.
    """
    return [key for key in ("RESTIC_REPOSITORY", "RESTIC_PASSWORD") if not env.get(key)]


def get_events_dir() -> Path | None:
    """Return $MNGR_AGENT_STATE_DIR/events/backup or None if state dir is unset."""
    state_dir = os.environ.get("MNGR_AGENT_STATE_DIR", "")
    if not state_dir:
        return None
    return Path(state_dir) / "events" / "backup"


# ---------------------------------------------------------------------------
# Default-template writer (called by bootstrap)
# ---------------------------------------------------------------------------


def write_default_restic_env_template(path: Path = RESTIC_ENV_PATH) -> bool:
    """Write a commented-out restic.env template if absent. Returns True if written."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_DEFAULT_RESTIC_ENV_TEMPLATE)
    try:
        path.chmod(0o600)
    except OSError:
        # Best-effort; failure to tighten perms shouldn't block template creation.
        pass
    return True


_DEFAULT_RESTIC_ENV_TEMPLATE: Final[str] = """# Restic backup repository + secrets.
#
# In the minds app this whole file is written for you when you pick a backup
# provider on the create form; you should not need to edit it by hand.
#
# host_backup will not run until both RESTIC_REPOSITORY and RESTIC_PASSWORD
# are set.
#
# RESTIC_REPOSITORY is the repository address restic backs up to, e.g.
# 's3:https://<account>.r2.cloudflarestorage.com/<bucket>' for Cloudflare R2.
#
# RESTIC_PASSWORD is this workspace's repository password.
#
# AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY are the credentials for an
# S3/R2 backend. Other backends read other env vars -- see
# https://restic.readthedocs.io/en/stable/040_backup.html#environment-variables

# RESTIC_REPOSITORY=
# RESTIC_PASSWORD=
# AWS_ACCESS_KEY_ID=
# AWS_SECRET_ACCESS_KEY=
"""


def render_default_backup_toml(snapshot: SnapshotSettings) -> str:
    """Render a fresh backup.toml document with bootstrap-detected snapshot section."""
    doc = tomlkit.document()
    doc.add(tomlkit.comment("Backup script configuration. Read by libs/host_backup."))
    doc.add(
        tomlkit.comment(
            "Settings in [snapshot] are overwritten by bootstrap on every boot;"
        )
    )
    doc.add(tomlkit.comment("all other sections are preserved if you edit them."))
    doc.add(tomlkit.nl())

    doc["backup_interval_seconds"] = 3600
    doc["minimum_backup_gap_seconds"] = 60
    doc["config_poll_interval_seconds"] = 15

    snapshot_table = _snapshot_to_toml_table(snapshot)
    doc["snapshot"] = snapshot_table

    doc.add(
        tomlkit.comment(
            "Repository + credentials live in runtime/secrets/restic.env (RESTIC_REPOSITORY,"
        )
    )
    doc.add(tomlkit.comment("RESTIC_PASSWORD, and any backend creds), not here."))

    retention = tomlkit.table()
    retention["keep_hourly"] = 24
    retention["keep_daily"] = 30
    retention["keep_weekly"] = 12
    retention["keep_monthly"] = 24
    retention["prune_interval_hours"] = 24
    doc["retention"] = retention

    excludes = tomlkit.array()
    for pattern in BackupConfig.model_fields["excludes"].default:
        excludes.append(pattern)
    excludes.multiline(True)
    doc["excludes"] = excludes

    return tomlkit.dumps(doc)


def _snapshot_to_toml_table(snapshot: SnapshotSettings) -> tomlkit.items.Table:
    """Render a SnapshotSettings into a tomlkit table, skipping None fields."""
    table = tomlkit.table()
    table["method"] = snapshot.method.value
    if snapshot.btrfs_mount_path is not None:
        table["btrfs_mount_path"] = str(snapshot.btrfs_mount_path)
    if snapshot.host_subvolume_path is not None:
        table["host_subvolume_path"] = str(snapshot.host_subvolume_path)
    if snapshot.snapshot_current_path is not None:
        table["snapshot_current_path"] = str(snapshot.snapshot_current_path)
    if snapshot.snapshot_read_path is not None:
        table["snapshot_read_path"] = str(snapshot.snapshot_read_path)
    if snapshot.trigger_dir is not None:
        table["trigger_dir"] = str(snapshot.trigger_dir)
    table["outer_helper_timeout_seconds"] = snapshot.outer_helper_timeout_seconds
    # max_local_snapshots only governs the outer_trigger retention loop; keep
    # it out of btrfs_local / direct configs so those stay untouched.
    if snapshot.method == SnapshotMethod.OUTER_TRIGGER:
        table["max_local_snapshots"] = snapshot.max_local_snapshots
    return table


def merge_snapshot_into_existing_toml(
    existing_text: str, snapshot: SnapshotSettings
) -> str:
    """Replace the `[snapshot]` table in `existing_text` with one derived from `snapshot`.

    Preserves all other sections + user-added comments. Used by bootstrap on
    every boot so an environment change (workspace restored on a different
    provider) gets its `snapshot.method` corrected automatically without
    clobbering the user's retention / excludes / repo URL edits.
    """
    doc = tomlkit.parse(existing_text)
    new_table = _snapshot_to_toml_table(snapshot)
    doc["snapshot"] = new_table
    return tomlkit.dumps(doc)
