# Dead worker recovery

When a worker (sub-agent created via `launch-task`) is in `STOPPED` state -- claude session died mid-iteration, but the worktree (and any uncommitted work in it) is still intact -- the default path is to restart it, not to manually salvage. `mngr start` only re-creates the tmux session and re-execs claude in the existing worktree; it does not touch git state, so uncommitted changes survive the restart.

## First: was the worker shed for memory pressure?

A worker can die because the **memory watchdog** shed it -- the container was running out of memory and the watchdog killed the most-expendable work first. Check the shed ledger before reviving, because reviving into ongoing memory pressure just gets the worker killed again:

```bash
# Did the watchdog shed this worker? (look for your worker's name)
grep '"agent_name": *"<worker>"' runtime/memory_watchdog/events/shed/events.jsonl

# Is the container still under pressure right now?
cat runtime/memory_watchdog/status.json   # is_under_pressure, used_fraction
```

Revival guidelines when a worker was shed:

- **If pressure is still elevated** (`is_under_pressure` is true, or `used_fraction` is near the threshold): do NOT revive. Surface the situation to the user and let them decide -- reviving now will likely just be shed again and deepen the crunch.
- **If pressure has cleared**: revive at most once (the default restart below) and re-establish your report poll.
- **If the same worker has already been shed twice** (two `process_shed` lines naming it): stop. Do not keep reviving. Surface to the user with the ledger details -- something about this worker's footprint is incompatible with the current memory budget.

If the worker was *not* in the ledger, it died for some other reason; proceed with the normal restart path below.

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
