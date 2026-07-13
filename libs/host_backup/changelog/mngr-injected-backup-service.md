Refactored host_backup so the minds desktop app can idempotently inject/update the service into running workspaces and verify it matches the expected version:

- Snapshot mechanics are now "backup capabilities" detected in memory by the service itself at startup (new `host_backup/capabilities.py`, same trigger-dir/findmnt decision tree bootstrap used); they are no longer configured via `backup.toml`. A `capabilities_detected` event is emitted once at service startup.

- `runtime/backup.toml` is now purely optional user settings (interval, retention, excludes) with built-in defaults. Loading is tolerant: unknown keys (including the stale `[snapshot]` section pre-refactor bootstraps keep writing) and malformed values are logged and skipped instead of crashing the service, and one malformed value never blocks the remaining valid settings. The config is re-parsed only when `backup.toml`'s mtime moves, so tolerant-parse warnings appear once per edit rather than every poll.

- A missing `runtime/secrets/restic.env` simply means backups are not configured; the minds app is the only writer (bootstrap no longer seeds a template).

- A config file *appearing* (or disappearing) now counts as a config change: since neither file is seeded anymore, the runner fires a prompt backup tick when minds first injects `restic.env` into a running workspace, or when `host-backup-now` creates an absent `backup.toml` -- instead of waiting out the full backup interval.

- `host_backup.config` keeps no-op backwards-compatibility shims (`SnapshotSettings`, `merge_snapshot_into_existing_toml`, `render_default_backup_toml`, `write_default_restic_env_template`) for the names pre-refactor bootstraps import at container boot; removable once all pre-refactor hosts rotate out.

- Documented the stable contract relied on by minds backup-service updates (the `[program:host-backup]` supervisord block, root pyproject registration, and `uv run host-backup` entry points never change via injection; updates land as `backup-update: minds-v<X>` commits).

- Removed the now-unused `tomlkit` dependency.
