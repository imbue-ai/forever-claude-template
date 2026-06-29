---
name: crystallize-artifact
description: "Promote the just-finished work into a new, reusable, committed, tested skill. Use when that process would recur with new inputs -- especially something you figured out through research or debugging -- or when the user says 'crystallize this'."
---

# Crystallizing an artifact into existence

This is the **create** lead of the generic artifact lifecycle. The user has
signed off on a shape -- a process from the just-finished turn, a confirmed data
sample, a confirmed web-service mock -- and you put in the thorough, expensive
pass that turns it into a hardened, committed, reviewed artifact, in the
background. You dispatch the build to a generic worker; your role is to package
context, launch, proxy gates, merge, and go live.

## The artifact parameter

Crystallize creates one of:

- **skill** (the default when invoked standalone post-turn): a reusable skill
  reconstructed from the transcript. This is the common case.
- **service**: a scaffolded web service `build-web-service` already built and
  the user confirmed live. `build-web-service` invokes this lead with
  `artifact=service`.
- A **skill from a confirmed data sample**: `fetch-process-show` invokes this
  lead with `artifact=skill` and a `source_artifacts_dir` of staged scripts +
  `sample.json`.

The artifact drives two things: which **gates** the worker emits (skill →
`outline-approval` then `final-artifact`; service → none, since the user
confirmed the live site already) and the **go-live** step after merge (skill →
post-crystallize migration; service → refresh the tab). The worker reads the
artifact from the task file and loads `artifact-<artifact>.md`; you proxy
whatever gates it emits.

## When to invoke (skill, standalone)

Read `references/when-to-crystallize.md` if you haven't, for the full
guidelines. Summary: invoke when (1) the just-finished work was a single
cohesive unit, (2) a re-run with different inputs would follow a recognizably
similar process, and (3) the task is likely to recur. Model-judgement steps in
the middle of a flow do not disqualify it -- they are scripted as `[ai-script]`
calls. **Default to asking the user**, not deciding silently; if you can name
any plausible skill shape, propose it and let them decide.

## Step 1: Confirm (skill, standalone only)

**Skip this when the user explicitly invoked crystallize** (typed
`/crystallize-artifact`, said "crystallize this / make a skill out of this" in
the prior turn) **or when a live-half wrapper invoked this lead** after the user
approved a sample/mock. In those cases go straight to Step 2.

Otherwise send a one-line pre-gate question via `send-user-message`:

> "I just did X and Y. Worth crystallizing into a reusable skill? (yes/no)"

Wait for the reply. If no, stop.

## Conventions

Pick a short kebab-case slug `$NAME` for the artifact (e.g. `migrate-config`).
If a wrapper handed you a slug (and a `source_artifacts_dir`), reuse it. Then:

- Worker agent name and branch: `crystallize-$NAME` / `mngr/crystallize-$NAME`
- Runtime dir: `runtime/harden/crystallize-$NAME/`
- Task file: `runtime/harden/crystallize-$NAME/task.md`

## Step 2: Open a tracking ticket

The ticket survives until the post-merge migration, so record its ID to disk:

```bash
mkdir -p runtime/harden/crystallize-$NAME
TICKET_ID=$(tk create "crystallize $NAME" -t task \
    --acceptance "task file written; worker launched; worker DONE; branch merged")
tk start "$TICKET_ID"
echo "$TICKET_ID" > runtime/harden/crystallize-$NAME/ticket_id.txt
```

## Step 3: Write the task file

The frontmatter carries `operation: crystallize`, the `artifact`, the worker
reporting fields (`lead_agent` / `finish_report_path` per
`.agents/shared/references/worker-reporting.md`), and an optional
`source_artifacts_dir`. The body *describes* the work and -- for a skill
reconstructed from the transcript -- anchors the worker's search with verbatim
quotes (the user's original ask, key decisions, tool outputs that defined the
recipe). Without anchors the worker scans the wrong region of your transcript.
Describe invariants and state constraints; do **not** enumerate subcommands,
flow steps, or argparse surfaces -- those are the worker's decisions.

```bash
{
cat << FRONTMATTER_EOF
---
lead_agent: $MNGR_AGENT_NAME
finish_report_path: runtime/harden/crystallize-$NAME/reports/report.md
operation: crystallize
artifact: skill
FRONTMATTER_EOF
# If a wrapper staged artifacts, add this line inside the frontmatter:
#   source_artifacts_dir: runtime/<calling-skill>/$NAME/
cat << FRONTMATTER_CLOSE
---
FRONTMATTER_CLOSE
cat << 'BODY_EOF'

# Task: crystallize the just-finished work into a reusable skill

## What was done
<2-5 sentences describing the work to crystallize: what the user asked for,
what you did, the recipe that emerged. This should make sense to someone who
has not seen the transcript.>

## Anchors (verbatim quotes)
The worker uses these to locate the relevant turns via `mngr transcript`.
Include the user's original request (verbatim) and 1-3 short quotes marking
distinctive moments (a key decision, a tool output that drove a step).
<paste quotes here, one per bullet, in original wording.>

## Preconditions and postconditions
<what must be true about the artifact's inputs before it runs, and its outputs
after. Describe the contract; do not prescribe subcommands or surfaces.>

## What to do
Use the installed `harden-worker` sub-skill. It reads `operation` and `artifact`
from this frontmatter and follows the matching references. When you reach a gate
or terminal status, push a report to the lead per its reporting protocol; the
destination is `finish_report_path`.

## Success criteria
- The artifact is committed on your branch, tested, and passes the review gates.
- For a reconstructed skill: the user approved the outline (Gate 1) and the
  final artifact (Gate 2), each via a pushed report.
BODY_EOF
} > runtime/harden/crystallize-$NAME/task.md
```

Set `artifact: service` (and adjust the body to point at the already-built lib)
when invoked by `build-web-service`. Fill in the real `## What was done` and
`## Anchors` -- do not leave the placeholders. If frontmatter sets
`source_artifacts_dir`, Step 4 pushes that directory to the worker too.

## Step 4: Launch the worker

```bash
uv run .agents/skills/launch-task/scripts/create_worker.py launch \
    --name crystallize-$NAME \
    --template subskill-worker \
    --runtime-dir runtime/harden/crystallize-$NAME/ \
    --task-file runtime/harden/crystallize-$NAME/task.md
```

The `subskill-worker` template installs the generic `harden-worker` sub-skill.
If the frontmatter sets `source_artifacts_dir`, `launch` pushes it too -- no
extra flag.

## Step 5: Background-poll for worker reports

Run the poll as a Bash tool call with `run_in_background: true`, then continue
with other work (subsequent skill steps, interface design, other user requests).
Reports surface as task notifications; handle them when they arrive.

```bash
# Run with Bash run_in_background: true.
uv run .agents/skills/launch-task/scripts/create_worker.py await \
    --task-file runtime/harden/crystallize-$NAME/task.md \
    --timeout 90m
```

You own this poll even if you reached this step from a wrapper and moved on to
other work -- without it, gate reports never reach the user and the worker
deadlocks. Follow `.agents/shared/references/lead-proxy.md` for gate decisions,
the "do not interrupt more recent user work" rule, and terminal-status handling.
Flow-specific substitutions:

- Worker name: `crystallize-$NAME`; branch: `mngr/crystallize-$NAME`
- Task file / poll path: `runtime/harden/crystallize-$NAME/task.md` /
  `runtime/harden/crystallize-$NAME/reports/report.md`
- Reports dir: `runtime/harden/crystallize-$NAME/reports/`;
  consumed: `runtime/harden/crystallize-$NAME/reports/consumed/`
- Gates: **skill** → `outline-approval` (Gate 1) and `final-artifact` (Gate 2);
  **service** → none (the worker merges straight to `done`).
- Terminal statuses: `done` (merge, then Step 6); `stuck` (failure flow per
  `launch-task/references/worker-failure.md`).

## Step 6: Go live

On `done`, after merging the worker's branch:

- **skill**: read and follow `references/post-crystallize-migration.md` before
  declaring crystallize done -- point consumers at the installed skill path,
  delete the stale runtime artifact dir, pick up any breaking renames the worker
  introduced, restart any caching service, and close the ticket recorded in
  `runtime/harden/crystallize-$NAME/ticket_id.txt`. Commit consumer changes as a
  separate commit.
- **service**: refresh the tab so the user sees the merged build
  (`python3 scripts/layout.py refresh <service-name>`), then close the ticket.

## Guidelines

- Never crystallize without explicit user go-ahead (a yes to Step 1, the
  explicit invocation, or a wrapper's confirmed handoff).
- Never crystallize a turn whose process would not repeat recognizably on a
  re-run. Model-judgement steps within a stable process do NOT disqualify it.
- The worker owns outline and implementation decisions; do not second-guess its
  structure unless something is clearly wrong.
