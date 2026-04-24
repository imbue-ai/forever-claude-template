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
- Task file path: `runtime/crystallize/$NAME/task.md` (sits alongside
  `turn.jsonl` so the existing Step 4 `mngr push` syncs it to the
  worker for free)
- `tk` ticket title

Use that same slug everywhere below.

## Step 1: Confirm and open a tracking ticket

**Skip the pre-gate question if the user explicitly invoked this skill.**
Triggers that count as explicit invocation: the user typed
`/crystallize-task`, said "crystallize this / yes crystallize / make a
skill out of this" in the immediately-prior turn, or otherwise named
the skill by hand. In that case go straight to the ticket -- asking
again is redundant and annoying.

Otherwise send a one-line pre-gate question via the `send-user-message` skill:

> "I just did X and Y. Worth crystallizing into a reusable skill? (yes/no)"

Wait for the user's reply. If no, stop here.

If the user said yes (or the skip rule above applied), open a `tk`
ticket so the lifecycle is visible after the turn ends:

```bash
if command -v tk >/dev/null 2>&1; then
    TICKET_ID=$(tk create "crystallize $NAME" -t task \
        --acceptance "transcript extracted; task file written; worker launched; worker DONE; branch merged")
    tk start "$TICKET_ID"
fi
```

If `tk` is not on PATH, skip tracking; the rest of the
skill is unaffected.

## Step 2: Extract the just-finished turn

See `.agents/shared/references/lead-proxy.md` for the `extract_turn.py`
invocation contract.

```bash
uv run .agents/shared/scripts/extract_turn.py \
    --nth 1 \
    --output runtime/crystallize/$NAME/turn.jsonl
```

## Step 3: Write the task file

Describe invariants and state constraints — what must be true about the
skill's inputs and outputs. Do not enumerate subcommands, flow steps, or
argparse surfaces; surface decisions belong to the worker.

The task file's YAML frontmatter follows the schema in
`.agents/shared/references/worker-reporting.md` -- `lead_agent`,
`lead_report_dir`, and `transcript_path`.

```bash
mkdir -p runtime/crystallize/$NAME
{
cat << FRONTMATTER_EOF
---
lead_agent: $MNGR_AGENT_NAME
lead_report_dir: runtime/crystallize/$NAME/reports/
transcript_path: runtime/crystallize/$NAME/turn.jsonl
---
FRONTMATTER_EOF
cat << 'BODY_EOF'

# Task: crystallize the just-finished work into a reusable skill

## Transcript
The turn you need to crystallize is at the path given by the
`transcript_path` frontmatter field (JSONL of tool calls and results).
Replay it mentally to understand what was done; you do not need to
re-execute destructive operations.

## Preconditions and postconditions
<describe what must be true about the skill's inputs before it runs, and
what must be true about its outputs after. Focus on the contract; do not
prescribe subcommands, flow steps, or argparse surfaces — the worker owns
those decisions.>

## What to do
Use the `crystallize-task-worker` sub-skill to drive the end-to-end
build. When you reach a gate or terminal status, write a report file
and push it to the lead per the sub-skill's reporting protocol; the
destination is given by `lead_agent` / `lead_report_dir` in
frontmatter.

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

The split-heredoc shape keeps variable expansion (`$MNGR_AGENT_NAME`,
`$NAME`) contained to the small frontmatter block while the larger
body block stays single-quoted -- so `$` and backticks in the body are
literal by default, no escaping needed.

## Step 4: Launch the worker

Follow the `launch-task` skill's conventions for worker lifecycle management
(background waiting, checking results, handling outcomes), with these
crystallize-specific overrides:

- Template: `-t crystallize-worker` (not `-t worker`)
- Task file: the one written in step 3

```bash
mngr create crystallize-$NAME -t crystallize-worker \
    --label workspace=$MINDS_WORKSPACE_NAME \
    --message-file runtime/crystallize/$NAME/task.md
```

Push the runtime dir (task file + transcript) into the worker's worktree --
see `.agents/shared/references/lead-proxy.md` § "mngr push rationale" for
why the directory form and `--uncommitted-changes=merge` are required:

```bash
mngr push crystallize-$NAME:runtime/crystallize/$NAME/ \
    --source runtime/crystallize/$NAME/ \
    --uncommitted-changes=merge
```

## Step 5: Proxy reports, then merge

Follow `.agents/shared/references/lead-proxy.md` for polling, gate
decisions, the "do not interrupt more recent user work" rule, `mngr push`
rationale, and terminal-status handling.

Flow-specific substitutions:

- Worker name: `crystallize-$NAME`
- Branch: `mngr/crystallize-$NAME`
- Poll path: `runtime/crystallize/$NAME/reports/report.md`
- Consumed path: `runtime/crystallize/$NAME/reports/consumed/`
- User-approval gates: `type: gate, name: outline-approval` (Gate 1) and
  `type: gate, name: final-artifact` (Gate 2).
- Terminal statuses: `type: status, name: done` (merge);
  `type: status, name: stuck` (failure-handling flow).

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
