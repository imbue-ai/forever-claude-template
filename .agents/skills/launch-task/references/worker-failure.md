# Worker failure handling

When a sub-agent you launched via `launch-task` (or a derivative skill like
`crystallize-task`, `heal-skill`, `update-skill`) finishes in a failed state --
or finishes DONE but produced the wrong result -- do **not** silently retry and
do **not** try to fix the worker's output inline. The main-agent layer is not
where worker bugs get fixed.

## What counts as a failure

- Worker state is `STOPPED` without reaching a normal completion.
- Worker state is `DONE` but its final message says it gave up, could not
  diagnose, or could not produce an artifact.
- Worker's branch is missing, empty, or contains a commit that clearly does
  not implement what the task file asked for.
- User rejected the worker's Gate 2 proposal and the worker stopped instead
  of iterating.
- For crystallize / heal / update workers: the worker's final assistant
  message begins with `## STATUS: stuck`, or terminated without any
  `## STATUS: <name>` header at all. The first case is the worker
  explicitly giving up (its prose names a reason); the second means the
  worker died without following its contract.

## What to do

1. **Capture context** while it's still available. Use whatever tools your
   runtime exposes (e.g. `mngr transcript <agent>`, `mngr capture <agent>`)
   to save the worker's final messages and any visible error output into the
   main-agent transcript so the user can read them.
2. **Tell the user** in plain language: what was supposed to happen, what
   happened instead, and where the evidence lives (branch name, transcript
   command). Keep it short -- the user decides the next step.
3. **Leave the worker's branch and tmux session intact** unless the user
   asks you to clean up. The evidence is more useful than the tidiness.
4. **Update any outstanding tickets** (e.g. `tk` lifecycle tickets) with a
   note describing the failure; do not close them -- leave them open so the
   user can resume.
5. **Move on**. Do not re-spawn the same worker with the same task file in
   the same turn. If you have a genuinely different approach the user
   hasn't seen, mention it and wait for the user to authorize the retry.

## When retrying is acceptable

Only if the failure was transient and unrelated to the worker's logic:

- `mngr create` itself returned an error (provisioning failed, not enough
  resources, etc.) -- retry the create, not the task.
- The worker hit a rate-limit or transient network error before doing any
  work -- the task file is still good, retry it verbatim.

If you aren't sure whether a failure was transient, surface it to the user
instead of guessing.

## Why

The main-agent layer has a narrow role: dispatch, observe, merge, report.
Deciding *why* a worker's script or SKILL.md misbehaved and applying a fix
is exactly the job of `heal-skill` / `update-skill` -- both of which expect
to run *after* the user has weighed in.
