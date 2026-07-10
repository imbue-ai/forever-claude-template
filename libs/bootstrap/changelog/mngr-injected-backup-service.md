Bootstrap no longer initializes the host-backup configuration:

- Removed the first-boot step that detected the snapshot mechanism and seeded `runtime/backup.toml` + a `runtime/secrets/restic.env` template. Snapshot mechanics are now detected in memory by the host_backup service itself at startup, `backup.toml` is purely optional user settings, and `restic.env` is written only by the minds app (a missing file means backups are not configured).

- Dropped the `host-backup` and `tomlkit` dependencies, which were only needed by the removed step.
