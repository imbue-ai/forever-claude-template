---
name: update-artifact
description: "Change an existing skill, service, or shared script/reference (under .agents/shared/) -- extend it, refactor it, or just verify it still works. Invoke at turn-end when a skill ran but you had to do extra repeatable work by hand, or when you and the user discussed a change to it and applied it live."
---

# Updating an existing artifact

This is the **change** lead of the generic artifact lifecycle. The artifact
already exists; you dispatch a generic worker to harden the change in the
background, proxy its gates, merge, and go live. There is one flow with a
**design-gate toggle** keyed on whether the change is already committed.

## The two origins

- **committed** -- you and the user discussed the change live and it is already
  committed on the branch. The design was approved organically in chat, so the
  worker skips the design gate, reads the committed diff as ground truth,
  verifies it, and presents the final gate.
- **emergent** -- a skill ran but additional *repeatable* work had to be done by
  hand, with no design conversation. The worker reconstructs the incident from
  your transcript, runs a design gate (Gate 1), implements, runs scenarios, and
  presents the final gate.

Pick **committed** when the user was explicitly in the design loop *and* the
change is already committed. Otherwise **emergent** (the safe default; its
Gate 1 re-surfaces the design for an approval pass).

"Repeatable" covers deterministic extensions (an extra flag, a new output
format), model-judgement extensions (an additional judgement step with a stable
recipe, scripted as `[ai-script]`), and executor meta-work. One-off creative or
exploratory work is NOT an update candidate.

## The artifact parameter

`artifact` is `skill`, `service`, or `system-interface`. It drives where the
worker looks (`artifact-<artifact>.md`) and the **go-live** strategy (Step 4):
skill → cross-reference sweep is part of the edit, nothing else; service →
refresh the tab; system-interface → the `update-system-interface` wrapper owns a
preview-before-merge and a `safe-reveal` go-live and calls into this flow for
the orchestration core only (see that skill).

## Conventions

Use `$TARGET` for the artifact (e.g. `migrate-config`, a service name). Then:

- Worker agent name and branch: `update-$TARGET` / `mngr/update-$TARGET`
- Runtime dir / task file: `runtime/harden/update-$TARGET/` /
  `runtime/harden/update-$TARGET/task.md`

## Step 1: Open a tracking ticket

```bash
mkdir -p runtime/harden/update-$TARGET
TICKET_ID=$(tk create "update $TARGET" -t task \
    --acceptance "task file written; worker launched; worker DONE; branch merged")
tk start "$TICKET_ID"
```

## Step 2: Capture artifacts and write the task file

For the **committed** origin, capture the commit metadata and full diff so the
worker has a convenience index (the change is also on its branch on disk):

```bash
COMMIT_RANGE="HEAD~1..HEAD"   # widen to cover all commits implementing the change
git log --format='%H %s' "$COMMIT_RANGE" > runtime/harden/update-$TARGET/commit.log
git log -p "$COMMIT_RANGE"    > runtime/harden/update-$TARGET/commit.diff
```

Write the task file. Frontmatter carries `operation: update`, the `artifact`,
and the worker reporting fields (per
`.agents/shared/references/worker-reporting.md`). The body carries the
`## Change origin` marker the worker dispatches on, plus origin-specific content:

```bash
cat > runtime/harden/update-$TARGET/task.md << TASK_EOF
---
lead_agent: $MNGR_AGENT_NAME
finish_report_path: runtime/harden/update-$TARGET/reports/report.md
operation: update
artifact: skill
---

# Task: update \`$TARGET\`

## Change origin
ORIGIN: emergent

## Incident summary (emergent) / Committed change (committed)
<emergent: 2-5 sentences -- what the user asked for, how \`$TARGET\` fell short,
and the additional repeatable work you did by hand.>
<committed: branch, commit range (see commit.log), a summary of what changed and
why, and the design rationale the worker checks the diff against.>

## Anchors (verbatim quotes)
The worker uses these with \`mngr transcript\` to locate the relevant turns.
<emergent: the user's request, the insufficient \`$TARGET\` output, and a quote
showing the manual follow-up. committed: 1-3 quotes that pinned the design.>

## What the updated artifact must do
<emergent only: the contract the artifact must honor after the change -- inputs
it should now accept, outputs it should now produce. Describe the new contract;
the incident is captured above.>

## What to do
Use the installed \`harden-worker\` sub-skill. It reads \`operation\`,
\`artifact\`, and the \`## Change origin\` marker, then follows the matching
references. Push reports to the lead per its reporting protocol.

## Success criteria
- The change is hardened, tested, and passes the review gates on your branch.
- The user approved the final artifact (Gate 2); for the emergent origin, also
  the outline (Gate 1).
TASK_EOF
```

Set `ORIGIN: committed` and `artifact:` as appropriate. Fill in the real
content; do not leave placeholders. The `## Change origin` marker is required --
the worker fails loudly if it is missing.

## Step 3: Launch the worker and poll

```bash
uv run .agents/skills/launch-task/scripts/create_worker.py launch \
    --name update-$TARGET \
    --template subskill-worker \
    --runtime-dir runtime/harden/update-$TARGET/ \
    --task-file runtime/harden/update-$TARGET/task.md
```

Then background-poll (`create_worker.py await --task-file ... --timeout 90m`,
`run_in_background: true`) and follow `.agents/shared/references/lead-proxy.md`.
Flow-specific substitutions:

- Worker name: `update-$TARGET`; branch: `mngr/update-$TARGET`
- Poll path: `runtime/harden/update-$TARGET/reports/report.md`; reports dir
  `runtime/harden/update-$TARGET/reports/`; consumed
  `runtime/harden/update-$TARGET/reports/consumed/`
- Gates: `outline-approval` (emergent only -- the design gate) and
  `final-artifact` (both).
- Terminal statuses: `done` (go live, Step 4); `no-update-needed` (no change --
  close the ticket, no merge); `stuck` (failure flow per
  `.agents/skills/launch-task/references/worker-failure.md`).

## Step 4: Merge and go live

On `done`, merge `mngr/update-$TARGET`, then go live by artifact:

- **skill**: nothing beyond the merge (the worker's cross-reference sweep is part
  of the change). If the target is a built-in upstream skill, note the local
  drift to reconcile later via `update-self` / `submit-upstream-changes`.
- **service**: refresh the tab (`python3 scripts/layout.py refresh
  <service-name>`).
- **system-interface**: do **not** merge or reveal here -- the
  `update-system-interface` wrapper drives preview-before-merge and the
  `safe-reveal` go-live. (That wrapper uses this flow for Steps 1-3 only.)

Then close the ticket:

```bash
tk close "$TICKET_ID" "Updated $TARGET -- worker branch merged."
```

## Gotchas

- Update is non-blocking -- the user's original request is already delivered;
  the update worker produces a quieter follow-up commit.
- The committed origin's worker may produce no new commits of its own if
  verification is clean. That is expected -- the substantive change is already
  on the branch from the live commit.
