# github_sync

Opt-in GitHub sync for a workspace. Nothing in this library is active by
default: the `github-sync` skill enables it for a workspace whose user asks
for it, and the skill (not this library) creates the dedicated **private**
GitHub repo and points `origin` at it.

Once enabled, three pieces work together:

1. **The service** (`uv run github-sync run`, supervised as
   `[program:github-sync]`): every 60 seconds it commits the contents of
   `runtime/` (Claude memory, ticket state, transcripts, app port registry,
   etc.) on the stable orphan branch `runtime-sync` -- checked out as a git
   worktree at `runtime/` -- and pushes it to `origin`.
2. **The git wiring** (`uv run github-sync wire-git`): global git config that
   rewrites `https://github.com/...` remotes to the latchkey gateway's git
   proxy and attaches the gateway auth headers, so a plain `git push` works
   for every checkout in the container (main repo, worker worktrees, the
   runtime worktree). The GitHub credential is injected server-side by the
   gateway; no token ever enters the container. The wiring also points
   `core.hooksPath` at `scripts/git_hooks`, activating the post-commit
   auto-push hook for every checkout.
3. **The post-commit hook** (`scripts/git_hooks/post-commit`, in the repo but
   inert until the hooks path is wired): auto-pushes the active branch of any
   checkout after each commit, so both main-agent and worker commits land on
   the GitHub remote without manual pushes.

## Behavior

- Sync is configured iff `github_sync.toml` exists at the repo root (it holds
  `repo_url`); the skill writes it. Without it the service idles.
- Tick interval: 60 seconds. Each tick: `git add -A`, commit (only if dirty)
  with message `runtime sync: <ISO-8601 UTC timestamp>`, push. All `git`
  operations target the runtime worktree.
- Pushes go through the latchkey gateway on the user's machine, falling back
  to the per-VPS secondary gateway (remote hosts only) when the user's
  machine is offline. A failed push is retried on the next tick; `--force` is
  never used (the service is the branch's only writer).
- **Private-only enforcement**: the service re-checks the repo's visibility
  through latchkey every 15 minutes; pushes are held until the first
  confirmed-private answer and halted whenever the repo is confirmed public.
  A re-check that fails outright (e.g. the gateway is offline -- in which
  case pushes would fail too) keeps the last confirmed answer and is retried
  every tick. The post-commit hook consults the same status and skips pushes
  during a halt.
- `runtime/secrets` is excluded via the worktree's own `.gitignore` (written
  at init), so e.g. the Cloudflare tunnel token never reaches the remote.
- Each tick first clears a stale `index.lock` from the runtime worktree if
  one is present and older than the tick interval (a killed git process never
  cleans up its own lock, and without this every later `git add` would fail
  identically and syncing would stop permanently and silently).
- On any git failure the service logs to stderr and `/tmp/github-sync.log`
  and tries again on the next tick. It mirrors machine-readable status to
  `/tmp/github-sync-status.json` (read by the hook and the skill).

## Restoring on a fresh container

If a workspace is recreated from a previously-synced repo, the synced-in
supervisord config already contains the `[program:github-sync]` block, but
the latchkey permissions and the container-local git wiring/worktree do not
carry over. The service self-heals: each tick it re-applies the wiring and
retries the worktree init, which fetches origin's `runtime-sync` branch and
materializes the prior `runtime/` state (memory, tickets, transcripts) as
soon as the user re-grants the GitHub permissions (the github-sync skill
walks them through it).

## CLI

```
uv run github-sync run               # the service loop (supervisord)
uv run github-sync wire-git          # apply gateway git config + hooks path
uv run github-sync unwire-git        # remove them (disable path)
uv run github-sync setup-worktree    # create/restore the runtime/ worktree
uv run github-sync check-visibility  # print private/public/unknown; rc!=0 unless private
uv run github-sync status            # config + latest service status as JSON
```
