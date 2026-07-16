"""Backup configuration loading: optional user settings + injected secrets.

Two on-disk inputs, both optional from the service's point of view:

- `runtime/backup.toml` -- purely *user* settings (interval, retention,
  excludes). Entirely optional: when absent the service runs on built-in
  defaults. Loading is deliberately tolerant: unknown keys (including the
  `[snapshot]` section old bootstraps keep writing forever) and malformed
  values produce log warnings and fall back to defaults -- they never crash
  the service and never block the remaining valid settings from applying.
  Rides the opt-in GitHub sync of runtime/ when that is enabled.
- `runtime/secrets/restic.env` -- restic's repository address + all secrets
  (`RESTIC_REPOSITORY`, `RESTIC_PASSWORD`, and any backend credentials restic
  reads from the environment, e.g. `AWS_ACCESS_KEY_ID` /
  `AWS_SECRET_ACCESS_KEY` for an S3/R2 backend). Written only by minds
  (injected whole); a missing file simply means backups are not configured.
  Gitignored.

Snapshot mechanics are NOT configuration: see `host_backup.capabilities`,
which the service detects in memory at startup.
"""

import os
import tomllib
from pathlib import Path
from typing import Final

from imbue.imbue_common.frozen_model import FrozenModel
from loguru import logger
from pydantic import Field, ValidationError

from host_backup.capabilities import BackupCapabilities
from host_backup.capabilities import (
    SnapshotMethod as SnapshotMethod,  # compat re-export
)

BACKUP_TOML_PATH: Final[Path] = Path("runtime/backup.toml")
RESTIC_ENV_PATH: Final[Path] = Path("runtime/secrets/restic.env")
PRUNE_TIMESTAMP_PATH: Final[Path] = Path("runtime/last-restic-prune")

# Top-level backup.toml keys that are known-stale rather than unknown: old
# bootstraps rewrite a `[snapshot]` section into backup.toml on every boot
# forever. It is ignored (capabilities are detected at runtime instead), at
# debug level so it doesn't spam warnings on every reload.
_KNOWN_STALE_KEYS: Final[tuple[str, ...]] = ("snapshot",)


class BackupConfigError(ValueError):
    """Raised when restic.env cannot be read (backup.toml loading never raises)."""


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
    """User-tunable backup settings loaded (tolerantly) from runtime/backup.toml."""

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
    """Load backup.toml tolerantly; always returns a usable config.

    Absent file -> all defaults. Unparseable file -> warning + all defaults.
    Unknown keys -> warning (debug for the known-stale `[snapshot]` section)
    and ignored. A field that fails validation -> warning and that field's
    default, while every other valid field still applies.
    """
    if not path.exists():
        return BackupConfig()
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.warning("Ignoring unparseable backup config at {}: {}", path, e)
        return BackupConfig()
    return _build_backup_config_tolerantly(raw, source=str(path))


def _build_backup_config_tolerantly(
    raw: dict[str, object], *, source: str
) -> BackupConfig:
    """Build a BackupConfig from raw toml data, warning on (and skipping) bad input."""
    known_field_names = set(BackupConfig.model_fields)
    accepted: dict[str, object] = {}
    for key, value in raw.items():
        if key in _KNOWN_STALE_KEYS:
            logger.debug(
                "Ignoring stale `{}` section in {} (capabilities are detected at runtime)",
                key,
                source,
            )
            continue
        if key not in known_field_names:
            logger.warning("Ignoring unknown backup config key `{}` in {}", key, source)
            continue
        accepted[key] = value

    # Validate field-by-field so one malformed value cannot take down the
    # rest of the user's settings.
    valid: dict[str, object] = {}
    for key, value in accepted.items():
        try:
            candidate = BackupConfig.model_validate({key: value})
        except ValidationError as e:
            logger.warning(
                "Ignoring invalid backup config value for `{}` in {}: {}",
                key,
                source,
                e,
            )
            continue
        valid[key] = getattr(candidate, key)
    return BackupConfig.model_validate(valid)


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
# Backwards-compatibility shims for pre-refactor bootstraps.
#
# Old workspaces keep their old `libs/bootstrap` forever (the minds backup
# update mechanism replaces only `libs/host_backup/**`), and that old
# bootstrap imports the names below from this module at container boot --
# a missing name would crash boot before supervisord starts. Each shim is a
# harmless no-op: templates are no longer written and `[snapshot]` is no
# longer maintained in backup.toml. Removable once every pre-refactor host
# has rotated out.
# ---------------------------------------------------------------------------

# Old bootstraps construct this to describe the detected snapshot mechanism;
# the capabilities model still carries every field they pass.
SnapshotSettings = BackupCapabilities


def merge_snapshot_into_existing_toml(
    existing_text: str, snapshot: BackupCapabilities
) -> str:
    """Compat no-op: returns the existing text unchanged (never rewrites `[snapshot]`)."""
    return existing_text


def render_default_backup_toml(snapshot: BackupCapabilities) -> str:
    """Compat shim: renders a comment-only pointer instead of a real default config."""
    return (
        "# Optional user settings for the host_backup service (interval, retention,\n"
        "# excludes). All settings have built-in defaults; this file may be deleted.\n"
        "# Snapshot mechanics are detected by the service at runtime and are not\n"
        "# configured here. See libs/host_backup/README.md.\n"
    )


def write_default_restic_env_template(path: Path = RESTIC_ENV_PATH) -> bool:
    """Compat no-op: restic.env templates are no longer written (minds injects the file)."""
    return False
