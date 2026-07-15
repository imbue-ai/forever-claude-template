GitHub sync is now opt-in via the new `github-sync` skill, replacing the always-on `runtime-backup` service.

By default, nothing pushes to GitHub anymore: the `runtime-backup` supervisord service, bootstrap's `runtime/` worktree init (the `mindsbackup/$MNGR_AGENT_ID` orphan branch), the always-installed `core.hooksPath` post-commit auto-push, and every use of `GH_TOKEN` have been removed. `runtime/` is a plain gitignored directory until sync is enabled; the restic `host-backup` service remains the default durability story.

The new `github-sync` skill enables sync on request: it creates a dedicated PRIVATE GitHub repo through latchkey (no token ever enters the container), points `origin` at it, wires plain `git push` through the latchkey gateway for every checkout (with the per-VPS secondary gateway as an offline fallback), installs the post-commit hook so all agent and worker commits auto-push their branch, and adds a `[program:github-sync]` service that syncs `runtime/` to the stable `runtime-sync` orphan branch every 60s. `libs/runtime_backup` was renamed and extended into `libs/github_sync` (`uv run github-sync run|wire-git|unwire-git|setup-worktree|check-visibility|status`).

Private-only is enforced: the skill refuses public repos at setup and the service re-verifies visibility every 15 minutes, halting all pushes (service and hook) when the repo is public or unverifiable.

The skill's permission ask was validated against a live workspace and corrected: creating the repo needs `github-write-all` (the narrower `github-write-repos` covers only existing-repo paths, not `POST /user/repos`), and `github-read-user` is requested so the skill can name the owning account and verify the grants landed. Both requests now go out back-to-back before any GitHub call, so the user approves once and setup runs to completion.

A workspace recreated from a previously-synced repo self-heals: after the user re-grants the GitHub permissions, the service re-wires git and restores `runtime/` (memory, tickets, transcripts) from the `runtime-sync` branch automatically. Disable is supported (full unwind of the service, hook, and git wiring; the user chooses whether to keep the remote repo).

Existing workspaces that update-self onto this version simply stop git-syncing `runtime/` until they opt in via the skill; legacy `mindsbackup/*` branches are not migrated.
