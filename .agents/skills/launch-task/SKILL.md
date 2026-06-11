---
name: launch-task
description: Create a sub-agent to perform a larger task. Use when work is large enough to warrant a separate context, involves multi-file changes, or benefits from isolation.
---

# Launching a task

Pick a short kebab-case slug `$NAME` for this dispatch (e.g.
`fix-login-bug`, `add-search-feature`). It is used for the worker name,
its branch (`mngr/$NAME`), and the local runtime path
(`runtime/launch-task/$NAME/`). Names must be unique.

## 0. Open a single tk step for the whole delegation

The progress view treats each delegation as **one** step in your timeline, regardless of how much work the sub-agent does internally. Before doing anything else, create one step record that describes the delegation in user-facing terms and start it. `tk create --step` prints `Created <id>: <title>`; use that id literally in `tk start`/`tk close`:

```bash
tk create --step "Delegate <plain-english description of what the sub-agent will do> to a sub-agent"
# -> Created cod-step-XXXX: Delegate ...
tk start cod-step-XXXX
```

The sub-agent will use its own `.tickets/` for its own internal progress — that work renders in the sub-agent's chat, not yours. Don't try to surface the sub-agent's individual steps in your timeline; the user can open the sub-agent's chat if they want that level of detail.

When the sub-agent finishes (Step 4 below), close your step. The closing summary describes the *work you did* — e.g. "Briefed a sub-agent on the dark-mode toggle fix and reviewed its result." — not the outcome. Save the actual outcome / result for your final assistant message to the user.

```bash
tk close cod-step-XXXX "Briefed a sub-agent on the <task> and reviewed its result."
```

## 1. Write the task file

Write a clear task file with YAML frontmatter (so the worker can address
reports back to you) followed by the human-readable task description.
The frontmatter contains `lead_agent` and `finish_report_path`.

```bash
mkdir -p runtime/launch-task/$NAME
{
cat << FRONTMATTER_EOF
---
lead_agent: $MNGR_AGENT_NAME
finish_report_path: runtime/launch-task/$NAME/reports/report.md
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
Follow `.agents/shared/references/worker-reporting.md` for the full
report procedure: it has you parse this task's frontmatter to get
`LEAD_AGENT` / `FINISH_REPORT_PATH`, then write the report file and push
its parent directory back to the lead. Substitutions for this task:

- `<TASK_FILE_GLOB>` -> `runtime/launch-task/*/task.md`
- `<RUNTIME_REPORTS_DIR>` -> the directory part of `finish_report_path`,
  i.e. `dirname "$FINISH_REPORT_PATH"` (your worktree path matches the
  lead's destination for this flow)
- Valid `name:` values: `question` (mid-flight gate), `done` / `stuck`
  (terminal).

For a mid-flight `question` gate, stop your turn after pushing -- the
lead replies via `mngr message` and you resume. For terminal statuses,
the run ends.
BODY_EOF
} > runtime/launch-task/$NAME/task.md
```

## 2. Launch the worker

`scripts/create_worker.py launch` runs the worker lifecycle: `mngr create`,
the runtime-dir push, and the task message. Run it in the foreground so a
failed launch surfaces immediately.

```bash
uv run .agents/skills/launch-task/scripts/create_worker.py launch \
    --name $NAME \
    --template worker \
    --runtime-dir runtime/launch-task/$NAME/ \
    --task-file runtime/launch-task/$NAME/task.md
```

If the task references gitignored files outside the runtime dir, set
`source_artifacts_dir: <dir>` in the task frontmatter; `launch`
pushes that directory into the worker's worktree automatically.

## 3. Background-poll for the worker's report

Poll with `create_worker.py await` as a background task
(`run_in_background: true`) and continue with whatever else you were doing. It
reads `finish_report_path` from the task file
(`runtime/launch-task/$NAME/reports/report.md`), blocks until the worker pushes
back, then prints the report.

```bash
# Run with Bash run_in_background: true
uv run .agents/skills/launch-task/scripts/create_worker.py await \
    --task-file runtime/launch-task/$NAME/task.md
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
- Task file (pass to `create_worker.py await --task-file`): `runtime/launch-task/$NAME/task.md`
- `finish_report_path`: `runtime/launch-task/$NAME/reports/report.md`
- Reports dir (for `<REPORTS_DIR>`, i.e. `dirname finish_report_path`): `runtime/launch-task/$NAME/reports/`
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
- If the task references gitignored files outside the runtime dir,
  declare them with `source_artifacts_dir: <dir>` in the task
  frontmatter -- `create_worker.py launch` pushes that directory automatically.
