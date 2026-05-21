# runtime_backup

Background service that periodically commits and pushes the contents of
`runtime/` (which holds Claude memory, ticket state, transcripts, telegram
history, app port registry, etc.) to a per-agent orphan branch named
`mindsbackup/$MNGR_AGENT_ID` on the same `origin` as the main checkout.

The branch and the worktree at `runtime/` itself are created by
`libs/bootstrap` during its pre-services init step. This service assumes
that has already happened and just polls.

## Behavior

- Tick interval: 60 seconds.
- Each tick: `git add -A`, commit (only if dirty) with message
  `runtime backup: <ISO-8601 UTC timestamp>`, push (only when `GH_TOKEN`
  is set in env). All `git` operations target the runtime worktree.
- Push first tries default args (no `--force`). Bootstrap normally sets
  the upstream during init, but if that best-effort push failed, the
  next tick's plain `git push` fails with "no upstream" -- so on push
  failure the runner retries once with `git push --set-upstream origin
  <branch>` to self-heal that case (mirroring the post-commit hook).
  `--force` is never used; the per-agent branch model means there is
  only ever one writer, so non-fast-forwards should not happen.
- `runtime/secrets` is excluded via the worktree's own `.gitignore`
  (written by bootstrap during init), so the Cloudflare tunnel token
  never reaches the remote.
- Each tick first clears a stale `index.lock` from the runtime worktree if
  one is present. This service is the worktree's only git writer and its
  ticks are sequential, so a lock here was normally left by a prior tick's
  git process that was killed before releasing it (if something kills the
  process mid-commit). Git never clears such a lock itself, so without this
  every later `git add` would fail identically and backups would stop
  permanently and silently. As a safeguard against the unlikely case of a
  concurrent writer, the lock is only removed once it is older than the tick
  interval -- a live git operation on the small `runtime/` tree holds the
  lock far more briefly than that, so an in-progress operation is never
  disturbed.
- On any git failure the service logs to stderr and `/tmp/runtime-backup.log`
  and tries again on the next tick. It does not exit, so bootstrap's
  `restart = "on-failure"` policy is only triggered on a hard crash.
- Without `GH_TOKEN` the service still commits locally; pushes are
  skipped until a token appears (typically on container restart).

## Restoring on a fresh container

If `mindsbackup/$MNGR_AGENT_ID` already exists on origin (e.g. the same
agent is recreated), bootstrap fetches and materializes it into `runtime/`
on first boot, so prior memory and runtime state come back automatically.

Migration to a *different* `MNGR_AGENT_ID` is intentionally manual.
