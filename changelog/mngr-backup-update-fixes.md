# Backup-service docs: official remote, minimum required version, and agent guidance

- `libs/host_backup/README.md`'s stable-contract section now documents that minds fetches `minds-v*` tags from a minds-owned `official` git remote (idempotently created/repointed at the canonical template URL, reserving the `upstream` name for update-self), and that drift detection compares against a fixed *minimum required* tag rather than the current app version.

- The `minds-api` skill now instructs agents to always create workspaces with backups unconfigured and to never ask the user for (or transmit) the backup master password; users enable backups from the minds desktop app afterwards.
