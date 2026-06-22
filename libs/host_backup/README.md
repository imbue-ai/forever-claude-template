# host_backup

Background service that continuously backs up the agent's full `host_dir`
(`/mngr/`) to a remote restic repository (Cloudflare R2 by default).

Distinct from `runtime_backup`, which only ships `runtime/` to a GitHub
orphan branch as a fine-grained checkpoint. `host_backup` covers the whole
host_dir (code, worktrees, agent state, chat sessions, logs) and pushes to
an encrypted restic repo on cheaper object storage.

## Behavior

- Single long-running tick loop run as the `host-backup` supervisord program
  (defined in `supervisord.conf`, started by supervisord after `bootstrap`).
  Restart policy: `autorestart=true`.
- The repository is created (and keyed) by the minds app, not by
  host_backup: minds runs `restic init` + `restic key add` from outside the
  workspace and injects the resulting `restic.env`. host_backup just backs up
  to the existing repository -- it does not probe-then-init.
- Each tick reads two config files written by `libs/bootstrap` (and, for
  `restic.env`, overwritten by minds):
  - `runtime/backup.toml`: non-secret settings -- backup interval, snapshot
    method, retention, exclude patterns.
  - `runtime/secrets/restic.env`: the repository address + all secrets --
    `RESTIC_REPOSITORY` (the only source of the repo URL), `RESTIC_PASSWORD`
    (this workspace's repository password), and any backend credentials
    restic reads from the environment (e.g. `AWS_ACCESS_KEY_ID` /
    `AWS_SECRET_ACCESS_KEY` for an S3/R2 backend). `restic.env` is gitignored
    (rides nothing). `backup.toml` is *not* gitignored so it survives
    container loss via runtime-backup.
- A tick only runs once both `RESTIC_REPOSITORY` and `RESTIC_PASSWORD` are
  set in `restic.env`. Backend credentials are not gated by host_backup --
  restic reports its own error if the chosen backend needs one that is
  missing.
- Snapshot method (set by bootstrap from the detected environment):
  - `btrfs_local`: take a `sudo btrfs subvolume snapshot -r` directly into
    `<btrfs-mount>/snapshots/current/` (lima).
  - `outer_trigger`: write a `request.json` into `/mngr-snapshot/` (a
    docker volume shared with the outer VPS) and wait for the outer
    `snapshot_helper.service` to drop a matching `result.json` (vps-docker).
    Each tick snapshots into a uniquely-named path
    `<btrfs-mount>/snapshots/<timestamp>` -- never a reused path. Under the
    sandbox's file gofer a reused path serves a stale, deleted subvolume, so
    only the first post-boot backup would capture data; unique names avoid
    that. After the backup, the oldest snapshots beyond `max_local_snapshots`
    (default 5, an `[snapshot]` knob) are deleted by name via a `cleanup`
    request that carries the snapshot name as `target`.
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
- `backup_started`, `snapshot_created`, `snapshot_deleted` (one per deleted
  snapshot -- `outer_trigger` may emit several per tick during keep-N pruning)
- `restic_backup_succeeded`, `restic_backup_failed`
- `forget_completed`, `prune_completed`, `prune_skipped`
- `config_reloaded`
- `tick_skipped_due_to_missing_secrets`, `tick_error`

Each restic command's full stdout / stderr is captured into the matching
`*_succeeded` / `*_failed` event for forensic debugging.

## First-run setup

In the minds app the whole `runtime/secrets/restic.env` is written for you
when you pick a backup provider on the create form -- minds initializes the
repository (`restic init` + `restic key add`) from outside the workspace and
injects the file. To configure backups by hand instead, populate
`runtime/secrets/restic.env` with `RESTIC_REPOSITORY` (e.g.
`s3:https://<account>.r2.cloudflarestorage.com/<bucket>`), the backend
credentials (e.g. R2 access keys), and a `RESTIC_PASSWORD`, and initialize
the repository yourself (`restic init`) before the first tick -- host_backup
does not create the repository.

## Restore

Out of scope for v1. To restore manually:

```
set -a; source /code/runtime/secrets/restic.env; set +a
restic snapshots
restic restore <snapshot_id> --target /tmp/restored
```
