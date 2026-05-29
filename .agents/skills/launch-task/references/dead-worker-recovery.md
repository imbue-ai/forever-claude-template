# Dead worker recovery

When a worker (sub-agent created via `launch-task`) is in `STOPPED` state -- claude session died mid-iteration, but the worktree (and any uncommitted work in it) is still intact -- the default path is to restart it, not to manually salvage. `mngr start` only re-creates the tmux session and re-execs claude in the existing worktree; it does not touch git state, so uncommitted changes survive the restart.

## Default: restart the worker and resume

1. Bring claude back up in the existing worktree:

   ```bash
   mngr start <worker>
   ```

2. Once it reaches `WAITING`, message it like any live agent -- ask it to continue, finish, or submit:

   ```bash
   mngr message <worker> -m "your previous run died. inspect git status and continue / submit as appropriate."
   ```

3. From here it's a normal worker again -- finalize via `submit-upstream-changes` when done.

## Last resort: manual worktree salvage

Only fall back to this path when the default doesn't apply: `mngr start` itself fails to bring the agent back, the worker is wedged in a way that another claude session can't unstick, or the agent has already been destroyed and you're recovering from its leftover worktree. In normal "claude crashed once" cases, restart instead.

1. Locate the worktree at `/mngr/worktree/<worker>-<hash>/` and inspect what's there:

   ```bash
   cd /mngr/worktree/<worker>-<hash>/
   git status
   git diff
   ```

2. Discard auto-generated lockfile churn so it doesn't ship alongside the substantive fix:

   ```bash
   git checkout HEAD -- vendor/mngr/uv.lock      # or whichever lockfile was touched
   ```

3. Stage only the substantive files and commit with a `WIP:` message that names the worker and notes that it was killed mid-iteration:

   ```bash
   git add <substantive-paths>
   git commit -m "WIP: <substantive summary> (worker <name> killed mid-iteration)"
   ```

4. Destroy the dead agent without dropping its branch:

   ```bash
   mngr destroy <worker> --force --no-allow-worktree-removal
   ```

   `--no-allow-worktree-removal` is what keeps the branch alive once the agent is gone.

5. The branch lives on. Finalize it like any other worker branch: cherry-pick onto your working branch, address ratchet/test fixups in follow-up commits, then push to `submit/<name>` per the `submit-upstream-changes` skill.
