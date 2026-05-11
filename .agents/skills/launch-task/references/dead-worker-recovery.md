# Dead worker recovery

When a worker (sub-agent created via `launch-task`) dies mid-iteration -- claude session killed, mngr shows `STOPPED`, but substantive uncommitted work is still in its worktree -- recover the work *before* destroying the agent. Destroying first removes the worktree and loses the changes.

## Recipe

1. Locate the worktree at `/worktree/<worker>-<hash>/` and inspect what's there:

   ```bash
   cd /worktree/<worker>-<hash>/
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
