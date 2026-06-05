---
name: crystallize-task
description: "Turn a process from the turn that just finished into a reusable skill. A skill captures a stable process -- SKILL.md prose describing the recipe, with scripts for deterministic steps and prose instructions for nondeterministic steps. Consider using after completing a task where a re-run with new inputs would follow a largely similar process. The process does not have to be the entire turn -- a sub-process (e.g. a data pipeline within a larger build) counts. Strong signal: you learned how to do something through research or debugging that is likely to be useful again."
---

# Crystallizing a task into a skill

Use this skill to promote ad-hoc work from the turn that just finished into a
reusable skill consisting of a PEP 723 `scripts/run.py` and a companion
`SKILL.md`, both [agentskills.io](https://agentskills.io/specification)-compliant.
You dispatch the actual build to a sub-agent; your role is to package context,
launch, and merge.

**Principle.** Reliability is the floor; simplicity is the target. Default to
a single entry point and one flow. Add surface only when a specific invariant
demands it. Decompose only when the separate components are likely to be used independently.

## When to invoke

Read `references/when-to-crystallize.md` if you haven't yet for detailed guidelines.

Summary:

1. The work was a single cohesive unit (not a mixed-bag turn that happened to
   touch many files or make many web requests or other tool uses).
2. **Re-run test**: if the user asked you to do this again with different
   inputs, much of the process would be recognizably the same -- same
   sources, same steps, same criteria, just different data. Judgement steps
   in the middle of a flow are fine; they live in SKILL.md as prose
   instructions.
3. You expect this task (or one like it) to recur, either because the user suggested it might or because it seems like a useful task to repeat.

A skill is a SKILL.md (process recipe) plus optional scripts for the
deterministic steps. Judgement steps live in SKILL.md as prose and are
executed by the agent using the skill. Do not demand end-to-end
scriptability before crystallizing.

**Default to asking the user**, not to deciding silently. If you can name
any plausible skill shape, propose it to the user and let them decide.
Only decline outright if the work truly has no stable process across
hypothetical re-runs.

**You don't have to crystallize the entire turn.** Look for reusable
sub-processes within the work. If you learned how to do something --
through research, debugging, or experimentation -- that seems likely to
be useful again, and the process would repeat recognizably, that's a
strong signal to crystallize it.

## Conventions

Pick a short kebab-case slug `$NAME` for this crystallization (e.g.
`migrate-config`). It is used for:

- Worker agent name: `crystallize-$NAME`
- Worker branch: `mngr/crystallize-$NAME` (created by `mngr create`)
- Local artifact paths under `runtime/crystallize/$NAME/`
- Task file path: `runtime/crystallize/$NAME/task.md` (the Step 4 push
  syncs it to the worker)
- `tk` ticket title

Use that same slug everywhere below.

If you were invoked from `do-something-new`, reuse its slug (`$SLUG`) as
`$NAME` -- the `source_artifacts_dir` frontmatter assumes matched paths.

## Step 1: Confirm

**Skip the pre-gate question if the user explicitly invoked this skill.**
Triggers that count as explicit invocation:

- The user typed `/crystallize-task`, said "crystallize this / yes
  crystallize / make a skill out of this" in the immediately-prior
  turn, or otherwise named the skill by hand.
- The calling skill is `do-something-new` (sample-approval at its Step 5 is the go-ahead).

In any of those cases go straight to Step 2 -- asking again is
redundant and annoying.

Otherwise send a one-line pre-gate question via the `send-user-message` skill:

> "I just did X and Y. Worth crystallizing into a reusable skill? (yes/no)"

Wait for the user's reply. If no, stop here.

## Step 2: Open a tracking ticket

The ticket survives until the post-merge migration in Step 6, so the ID
goes to disk under the runtime dir for that step to read back.

```bash
mkdir -p runtime/crystallize/$NAME
TICKET_ID=$(tk create "crystallize $NAME" -t task \
    --acceptance "task file written; worker launched; worker DONE; branch merged")
tk start "$TICKET_ID"
echo "$TICKET_ID" > runtime/crystallize/$NAME/ticket_id.txt
```

## Step 3: Write the task file

The worker will explore your transcript via `mngr transcript` to find
the work being crystallized. Your job here is to write a task body that
*describes* the work and anchors the worker's search with verbatim
quotes from the conversation (the user's original ask, key decisions,
tool calls or outputs that defined the recipe). Without anchors the
worker will scan the wrong region of your transcript.

Also describe invariants and state constraints — what must be true
about the skill's inputs and outputs. Do not enumerate subcommands,
flow steps, or argparse surfaces; surface decisions belong to the
worker.

The task file's YAML frontmatter follows the schema in
`.agents/shared/references/worker-reporting.md` -- `lead_agent` and
`finish_report_path`.

```bash
{
cat << FRONTMATTER_EOF
---
lead_agent: $MNGR_AGENT_NAME
finish_report_path: runtime/crystallize/$NAME/reports/report.md
---
FRONTMATTER_EOF
cat << 'BODY_EOF'

# Task: crystallize the just-finished work into a reusable skill

## What was done
<2-5 sentences describing the work to crystallize: what the user asked
for, what you did, the recipe that emerged. This is the worker's
primary guide -- it should make sense even to someone who has not seen
the transcript.>

## Anchors (verbatim quotes)
The worker will use these to locate the relevant turns in your
transcript via `mngr transcript`. Include:
- The user's original request (verbatim).
- 1-3 short quotes that mark distinctive moments (a key decision, a
  tool output that drove a step, a clarification the user gave).
<paste quotes here, one per bullet, in original wording.>

## Source artifacts (optional)
If your frontmatter has a `source_artifacts_dir` field, the calling
skill has pre-staged scripts and sample data at that path in your
worktree. Read those before designing the new skill so you reuse
working code instead of rebuilding from scratch.

## Preconditions and postconditions
<describe what must be true about the skill's inputs before it runs, and
what must be true about its outputs after. Focus on the contract; do not
prescribe subcommands, flow steps, or argparse surfaces — the worker owns
those decisions.>

## What to do
Use the `crystallize-task-worker` sub-skill to drive the end-to-end
build. When you reach a gate or terminal status, write a report file
and push it to the lead per the sub-skill's reporting protocol; the
destination is given by `lead_agent` / `finish_report_path` in
frontmatter.

## Data-capture guidance
When the skill being built fetches data from external APIs, capture
*all reasonable fields per record* in the calls you're already making,
not just the fields the user displayed in the original turn. This keeps
downstream consumers (e.g. an interface built later on top of the
captured data) unconstrained. Pagination is a normal part of the
workflow if the original ask requires it. Do NOT make extra
un-asked-for API calls just to gather more data.

Go further than fields: **persist the raw payload of each record and a
reference to its source, durably** (e.g. under `runtime/<name>/`), not
just the extracted/processed fields (see the preserve-and-surface
principle in CLAUDE.md for what "raw payload" and "source reference"
mean). A pipeline that fetches, transforms, and discards the raw payload
cannot satisfy that principle no matter what consumers do: persisting it
is what lets a *later* change in processing requirements re-derive new
fields with no refetch, and what lets surfaces show the raw record or
link out to the source. Make this a postcondition of the skill's
data-capture step.

## Worker sub-skills
The `crystallize-task-worker`, `heal-skill-worker`, and
`update-skill-worker` skills have been pre-installed into your
`.agents/skills/` tree.

## Success criteria
- New skill lives at `.agents/skills/<name>/` with SKILL.md
  (agentskills.io-compliant, `metadata.crystallized: true`) and
  `scripts/run.py` (PEP 723, argparse).
- All hand-crafted scenarios pass when run against `scripts/run.py`.
- User has approved both the outline (Gate 1) and the final artifact
  (Gate 2), each communicated via a pushed report file.
- Work is committed to your branch.
BODY_EOF
} > runtime/crystallize/$NAME/task.md
```

Fill in the `## What was done` and `## Anchors` sections with real
content drawn from your conversation -- do not leave the placeholders.

**Optional: source artifacts handoff.** If a calling skill (e.g.
`/do-something-new`) handed you a directory of pre-existing artifacts
(scripts, sample data) that the worker should have access to, add an
extra line to the frontmatter heredoc above:

```
source_artifacts_dir: runtime/<calling-skill>/<slug>/
```

Step 4 then pushes that directory to the worker alongside the standard
crystallize runtime dir.

## Step 4: Launch the worker

```bash
uv run .agents/skills/launch-task/scripts/create_worker.py launch \
    --name crystallize-$NAME \
    --template crystallize-worker \
    --runtime-dir runtime/crystallize/$NAME/ \
    --task-file runtime/crystallize/$NAME/task.md
```

If the task frontmatter sets `source_artifacts_dir`, `create_worker.py launch`
pushes that directory to the worker too -- no extra flag needed.

## Step 5: Background-poll for worker reports (concurrent with other work)

Run this poll command as a Bash tool call with `run_in_background: true`,
then continue with whatever else you were doing -- subsequent skill
steps, interface design, or other user requests. Reports surface as
task notifications when they arrive; handle them at that point, not by
blocking on the poll.

```bash
# Run with Bash run_in_background: true. Substitute $NAME with the slug.
uv run .agents/skills/launch-task/scripts/create_worker.py await \
    --task-file runtime/crystallize/$NAME/task.md \
    --timeout 90m
```

You still own this poll even if you reached this step from another skill
(e.g. `do-something-new`) and now move on to unrelated work. Do not
assume "fire and forget" -- without the poll, Gate 1 / Gate 2 reports
never reach the user and the worker deadlocks waiting for approval.

Follow `.agents/shared/references/lead-proxy.md` for gate decisions, the
"do not interrupt more recent user work" rule, `mngr rsync` rationale, and
terminal-status handling. Flow-specific substitutions:

- Worker name: `crystallize-$NAME`
- Branch: `mngr/crystallize-$NAME`
- Task file (pass to `create_worker.py await --task-file`): `runtime/crystallize/$NAME/task.md`
- `finish_report_path` / poll path: `runtime/crystallize/$NAME/reports/report.md`
- Reports dir (`<REPORTS_DIR>` = `dirname finish_report_path`): `runtime/crystallize/$NAME/reports/`
- Consumed path: `runtime/crystallize/$NAME/reports/consumed/`
- User-approval gates: `type: gate, name: outline-approval` (Gate 1) and
  `type: gate, name: final-artifact` (Gate 2).
- Terminal statuses: `type: status, name: done` (merge, then run Step 6);
  `type: status, name: stuck` (failure-handling flow).

## Step 6: Post-merge migration

Once `type: status, name: done` arrives and you have merged the
worker's `mngr/crystallize-$NAME` branch into the calling agent's
branch, **read and follow `references/post-crystallize-migration.md`
before declaring crystallize done**.

The migration covers: pointing consumers at the installed skill path
(replacing references to the runtime fallback), deleting the now-stale
runtime artifact dir, picking up any breaking renames the worker
introduced during autofix, restarting any service that was caching the
old path, and closing the tracking ticket recorded in
`runtime/crystallize/$NAME/ticket_id.txt`. The doc is short -- skip
items that don't apply.

If the migration produces consumer changes, commit them as a separate
commit so the migration is reviewable on its own.

## Guidelines

- Never crystallize without explicit user go-ahead. That go-ahead is
  either a Yes to the Step 1 pre-gate question or, if Step 1's skip
  rule applied, the explicit invocation itself (typing
  `/crystallize-task`, saying "crystallize this", etc.).
- Never crystallize a turn whose process would not repeat recognizably on a
  re-run. If each hypothetical re-run would require entirely different
  steps rather than the same recipe with different data, decline. Note
  that judgement steps within an otherwise stable process do NOT
  disqualify crystallization -- they live in SKILL.md as prose.
- The worker owns outline and implementation decisions. Do not second-guess
  the worker's skill structure unless something is clearly wrong.
- Worker failure handling: see `launch-task/references/worker-failure.md`.
