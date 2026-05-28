# host_backup

Background service that continuously backs up the agent's full `host_dir`
(`/mngr/`) to a remote restic repository (Cloudflare R2 by default).

Distinct from `runtime_backup`, which only ships `runtime/` to a GitHub
orphan branch as a fine-grained checkpoint. `host_backup` covers the whole
host_dir (code, worktrees, agent state, chat sessions, logs) and pushes to
an encrypted restic repo on cheaper object storage.

## Behavior

- Single long-running tick loop in tmux window `svc-host-backup` (started by
  the bootstrap service manager via `[services.host-backup]`). Restart
  policy: `on-failure`.
- Each tick reads two config files written by `libs/bootstrap`:
  - `runtime/backup.toml`: backup interval, snapshot method, retention,
    exclude patterns, repo URL template.
  - `runtime/secrets/restic.env`: `RESTIC_PASSWORD`, `AWS_ACCESS_KEY_ID`,
    `AWS_SECRET_ACCESS_KEY`. `restic.env` is gitignored (rides nothing).
    `backup.toml` is *not* gitignored so it survives container loss via
    runtime-backup.
- Snapshot method (set by bootstrap from the detected environment):
  - `btrfs_local`: take a `sudo btrfs subvolume snapshot -r` directly into
    `<btrfs-mount>/snapshots/current/` (lima).
  - `outer_trigger`: write a `request.json` into `/mngr-snapshot/` (a
    docker volume shared with the outer VPS) and wait for the outer
    `snapshot_helper.service` to drop a matching `result.json` (vps-docker).
  - `direct`: no snapshot; restic reads `/mngr/` directly (plain docker;
    intended for testing).
- Restic is run with `--exclude` for each entry in `backup.toml`'s
  `excludes` list (default: `**/.venv`, `**/node_modules`, etc).
- After every successful backup, `restic forget --keep-hourly N --keep-daily
  M --keep-weekly W --keep-monthly O` runs (cheap, index-only). At most
  once per `prune_interval_hours` (default 24) we additionally run
  `restic prune` (the slow data deletion step); gated by
  `runtime/last-restic-prune` (a timestamp file that rides runtime-backup
  so it survives container loss).
- The outer loop never exits. Every exception is logged with full traceback
  to loguru and as a `tick_error` event in the jsonl stream; the loop
  continues to the next tick.
- A hard `minimum_backup_gap_seconds` (default 60) gap is enforced between
  successive backup attempts, so a config that's being mutated constantly
  cannot spam restic / the error log.

## Reactive config reloading

The script polls `backup.toml` and `restic.env`'s mtimes every
`config_poll_interval_seconds` (default 15). If either file changed since
the last reload, the next tick fires immediately (subject to the minimum
gap). While a tick is running, polling is suspended; the script re-checks
once on completion and starts the next tick if either mtime advanced
during the run.

## Manual trigger

`uv run host-backup-now` waits for any in-progress backup to finish (so
your latest changes are guaranteed to be captured), bumps `backup.toml`'s
mtime, then tails `events/backup/events.jsonl` for the next
`restic_backup_succeeded` / `restic_backup_failed` event and prints it.

## Events

Structured events at `$MNGR_AGENT_STATE_DIR/events/backup/events.jsonl`:
- `backup_started`, `snapshot_created`, `snapshot_deleted`
- `restic_backup_succeeded`, `restic_backup_failed`
- `forget_completed`, `prune_completed`, `prune_skipped`
- `config_reloaded`, `repo_init_attempted`, `repo_init_succeeded`
- `tick_skipped_due_to_missing_secrets`, `tick_error`

Each restic command's full stdout / stderr is captured into the matching
`*_succeeded` / `*_failed` event for forensic debugging.

## First-run setup

1. The user populates `runtime/secrets/restic.env` with their R2 access
   keys + a chosen `RESTIC_PASSWORD`.
2. The user fills in the `[restic]` `account_id` / `bucket` fields in
   `runtime/backup.toml`.
3. On the next tick, `host-backup` probes the repo with
   `restic snapshots`; if it gets the "repository does not exist" error,
   it runs `restic init` and proceeds.

## Restore

Out of scope for v1. To restore manually:

```
source /code/runtime/secrets/restic.env
export RESTIC_REPOSITORY=s3:https://<account_id>.r2.cloudflarestorage.com/<bucket>/<host_id>
restic snapshots
restic restore <snapshot_id> --target /tmp/restored
```
