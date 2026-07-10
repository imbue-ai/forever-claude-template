Refactored the host_backup service so the minds desktop app can idempotently inject/update it into running workspaces and verify it matches the expected version:

- Snapshot mechanics are now "backup capabilities" detected in memory by the service itself at startup (new `host_backup/capabilities.py`); they are no longer configured via `backup.toml` and no longer written by bootstrap.

- `runtime/backup.toml` is now purely optional user settings (interval, retention, excludes) with built-in defaults, and loading is tolerant: unknown keys (including the stale `[snapshot]` section old bootstraps keep writing) and malformed values are logged and skipped instead of crashing the service.

- Bootstrap no longer seeds `backup.toml` or a `restic.env` template; a missing `runtime/secrets/restic.env` simply means backups are not configured (minds is the only writer).

- `host_backup.config` keeps no-op backwards-compatibility shims for the names pre-refactor bootstraps import at boot (removable once all pre-refactor hosts rotate out).

- Documented the stable contract relied on by minds backup-service updates (the `[program:host-backup]` supervisord block, root pyproject registration, and `uv run host-backup` entry points never change via injection; updates land as `backup-update: minds-v<X>` commits).
