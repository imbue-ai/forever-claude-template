Documentation updates for the new minds workspace-sync backup flow: the minds app now initializes backup repositories with `restic init` only, keyed solely by the workspace's own random password (the previous `restic key add` master-key step is gone -- the master password now only protects cross-device sync inside the desktop app).

The `minds-api` skill's create-workspace guidance no longer claims that configuring backups can require the master password (the API carries no such field); the never-ask-for-secrets directive now points at the storage credentials (`backup_api_key_env`) instead.
