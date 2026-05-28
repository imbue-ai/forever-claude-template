---
name: launch-task
description: Create a sub-agent to perform a larger task. Use when work is large enough to warrant a separate context, involves multi-file changes, or benefits from isolation. Additionally use when the user asks for any sort of AI integration in a service; use the process here to implement that integration.
---

# Launching a task

Pick a short kebab-case slug `$NAME` for this dispatch (e.g.
`fix-login-bug`, `add-search-feature`). It is used for the worker name,
its branch (`mngr/$NAME`), and the local runtime path
(`runtime/launch-task/$NAME/`). Names must be unique.

## 0. Open a single tk step for the whole delegation

The progress view treats each delegation as **one** step in your timeline, regardless of how much work the sub-agent does internally. Before doing anything else, create one step record that describes the delegation in user-facing terms and start it:

```bash
ID=$(tk create --step "Delegate <plain-english description of what the sub-agent will do> to a sub-agent")
tk start "$ID"
```

The sub-agent will use its own `.tickets/` for its own internal progress — that work renders in the sub-agent's chat, not yours. Don't try to surface the sub-agent's individual steps in your timeline; the user can open the sub-agent's chat if they want that level of detail.

When the sub-agent finishes (Step 5 below), close your step. The closing summary describes the *work you did* — e.g. "Briefed a sub-agent on the dark-mode toggle fix and reviewed its result." — not the outcome. Save the actual outcome / result for your final assistant message to the user.

```bash
tk close "$ID" "Briefed a sub-agent on the <task> and reviewed its result."
```

## 1. Write the task file

Write a clear task file with YAML frontmatter (so the worker can address
reports back to you) followed by the human-readable task description.
The frontmatter contains `lead_agent` and `lead_report_dir`.

```bash
mkdir -p runtime/launch-task/$NAME
{
cat << FRONTMATTER_EOF
---
lead_agent: $MNGR_AGENT_NAME
lead_report_dir: runtime/launch-task/$NAME/reports/
---
FRONTMATTER_EOF
cat << 'BODY_EOF'

# Task: <title>

## What to do
<description of what needs to be done and why>

## Context
<any relevant context: file paths, prior attempts, constraints>

## Success criteria
<what "done" looks like -- be specific>

## Reporting back
When you reach a terminal state (success or stuck) or have a
mid-flight question that blocks progress, write a single
`report.md` to the directory given by the `lead_report_dir`
frontmatter field above (resolved relative to your worktree --
the lead has already pushed that directory into your worktree
before sending this task; create the directory yourself with
`mkdir -p` if it does not yet exist). Frontmatter shape:

    ---
    type: status   # or `gate` for a mid-flight question
    name: done     # or `stuck` for a terminal failure; or `question` for a gate
    ---

    <body: address the user directly; one short paragraph for terminal
    statuses, the question itself for gate reports>

Then push the report directory back to the lead:

    mngr push <lead_agent>:<lead_report_dir> \
        --source <lead_report_dir> \
        --uncommitted-changes=merge

(Substitute the actual values from the frontmatter; the trailing
slashes matter, and `--uncommitted-changes=merge` is required because
the lead's worktree usually has uncommitted state.) For a mid-flight
gate, stop your turn after pushing -- the lead will reply via
`mngr message` and you resume. For terminal statuses, the run ends.
BODY_EOF
} > runtime/launch-task/$NAME/task.md
```

## 2. Dispatch the worker

`scripts/dispatch.py` runs the lifecycle commands -- `mngr create` (no
`--message-file` to avoid racing with the push), `mngr push` of the
runtime dir, optional extra pushes for gitignored auxiliary state, and
`mngr message` of the task file. Sending the message *after* the push
guarantees the worker sees the runtime dir before reading the task.

```bash
uv run .agents/skills/launch-task/scripts/dispatch.py \
    --name $NAME \
    --template worker \
    --runtime-dir runtime/launch-task/$NAME/ \
    --task-file runtime/launch-task/$NAME/task.md
```

If the task references other gitignored files (datasets, credentials,
extra transcripts), pass them as `--extra-push <dir>/` (repeatable) so
they land in the worker's worktree alongside the runtime dir.

## 3. Background-poll for the worker's report

Launch the poll as a background task (`run_in_background: true`) and
continue with whatever else you were doing. The report file appears at
`runtime/launch-task/$NAME/reports/report.md` once the worker pushes
back.

```bash
# Run with Bash run_in_background: true
timeout 30m bash -c '
  while [ ! -f runtime/launch-task/'"$NAME"'/reports/report.md ]; do sleep 5; done
  cat runtime/launch-task/'"$NAME"'/reports/report.md
'
```

You own this poll for the lifetime of the dispatch. Without it, gate
reports never reach the user and the worker deadlocks waiting for a
reply. Reports surface as task notifications when the background job
completes; handle them at that point, not by blocking on the poll.

## 4. Handle the report

Follow `.agents/shared/references/lead-proxy.md` for parsing the
report's frontmatter (`type` + `name`), deciding whether to answer a
gate yourself vs. escalate to the user, consuming the report so the
next push can land a fresh `report.md`, and acting on terminal statuses
(`done` -> merge the worker's branch; `stuck` or 30m timeout without a
report -> diagnose worker liveness, then surface to the user per
`references/worker-failure.md` if the worker is genuinely wedged).

Flow-specific substitutions when reading `lead-proxy.md`:

- Worker name: `$NAME`
- Branch: `mngr/$NAME`
- Reports dir: `runtime/launch-task/$NAME/reports/`
- Consumed dir: `runtime/launch-task/$NAME/reports/consumed/`
- Gate names: `question` (mid-flight; default-escalate to the user
  unless you can answer from context).
- Terminal statuses: `done` (merge); `stuck` (failure flow).

## Guidelines

- Always include clear success criteria in the task description.
- Background-poll the report file -- never block on it. The reply you
  need is a file appearing on disk, not the worker process exiting.
- Do not use `mngr wait` for completion signaling on these workers;
  it does not interact reliably with stop hooks or worker-spawned
  sub-agents. The report file is the contract.
- If a task fails (stuck report, or 30m poll timeout with no report and
  the worker is dead), see `references/worker-failure.md` -- do not
  silently retry.
- If a worker is `STOPPED` with uncommitted work, default to `mngr start
  <worker>` and message it to continue -- the worktree is preserved
  across restart. See `references/dead-worker-recovery.md` for the
  manual salvage fallback when restart isn't viable.
- If the task references gitignored files beyond the runtime dir, pass
  them as `--extra-push <dir>/` (repeatable) to `dispatch.py` so they
  land in the worker's worktree before it reads the task message.
