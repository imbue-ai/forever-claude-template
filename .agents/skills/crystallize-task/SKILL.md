---
name: crystallize-task
description: "Turn a reusable, mostly-deterministic process from the turn that just finished into a skill. The process does not have to be the entire turn -- a sub-process (e.g. a data pipeline within a larger build) counts. Strong signal: you learned how to do something through research or debugging that is likely to be useful again."
---

# Crystallizing a task into a skill

Use this skill to promote ad-hoc work from the turn that just finished into a
reusable skill consisting of a PEP 723 `scripts/run.py` and a companion
`SKILL.md`, both [agentskills.io](https://agentskills.io/specification)-compliant.
You dispatch the actual build to a sub-agent; your role is to package context,
launch, and merge.

**Principle.** Reliability is the floor; simplicity is the target. Default to
a single entry point and one flow. Add surface only when a specific invariant
demands it.

## When to invoke

The Stop hook emits a reminder whenever the turn used at least five non-read
tool calls. That threshold is deliberately dumb -- you supply the judgement.
Only crystallize when ALL of these hold:

1. The work was a single cohesive unit (not a mixed-bag turn that happened to
   touch many files).
2. The underlying process is mostly deterministic -- it could be expressed as
   a script with clear inputs and outputs.
3. You expect this task to recur, either verbatim or with minor input changes.

If none of the above holds for any portion of the turn, just send a short
acknowledgement to the user and move on. A pure-research turn, a one-off
incident response, or creative writing should never be crystallized.

**Important:** You don't have to crystallize the entire turn. Look for
reusable sub-processes within the work. In particular, if you learned
how to do something -- through research, debugging, or experimentation
-- that seems likely to be useful in the future, and the process is
mostly deterministic, that is a strong signal to crystallize it. Extract
just the reusable portion, even if the surrounding task was one-off.

## Conventions

Pick a short kebab-case slug `$NAME` for this crystallization (e.g.
`migrate-config`). It is used for:

- Worker agent name: `crystallize-$NAME`
- Worker branch: `mngr/crystallize-$NAME` (created by `mngr create`)
- Local artifact paths under `runtime/crystallize/$NAME/`
- Task file path: `/tmp/task-crystallize-$NAME.md`
- `tk` ticket title

Use that same slug everywhere below.

## Step 1: Confirm and open a tracking ticket

Send a one-line pre-gate question via the `send-user-message` skill:

> "I just did X and Y. Worth crystallizing into a reusable skill? (yes/no)"

Wait for the user's reply. If no, stop here.

If yes, open a `tk` ticket so the lifecycle is visible after the turn ends:

```bash
if command -v tk >/dev/null 2>&1; then
    TICKET_ID=$(tk create "crystallize $NAME" -t task \
        --acceptance "transcript extracted; task file written; worker launched; worker DONE; branch merged")
    tk start "$TICKET_ID"
fi
```

If `tk` is not on PATH (older containers), skip tracking; the rest of the
skill is unaffected.

## Step 2: Extract the just-finished turn

```bash
uv run .agents/skills/crystallize-task/scripts/extract_turn.py \
    --nth 1 \
    --output runtime/crystallize/$NAME/turn.jsonl
```

The helper auto-discovers the current session transcript via
`$CLAUDE_TRANSCRIPT_PATH` (set inside hooks) or `$MNGR_CLAUDE_SESSION_ID`.
Do not pass `--transcript` unless you have a specific file to replay.

`--nth 1` selects the *previous* human turn -- the one the user wants
crystallized. `--nth 0` (the default) would select the current
crystallize-task invocation turn itself, which is not what you want.

If counting turns does not line up cleanly (e.g. sub-agent interleaving),
use `--start-marker TEXT` and optionally `--end-marker TEXT` to slice by
matching text content instead.

## Step 3: Write the task file

Describe invariants and state constraints — what must be true about the
skill's inputs and outputs. Do not enumerate subcommands, flow steps, or
argparse surfaces; surface decisions belong to the worker.

```bash
cat > /tmp/task-crystallize-$NAME.md << 'TASK_EOF'
# Task: crystallize the just-finished work into a reusable skill

## Transcript
The turn you need to crystallize is at
runtime/crystallize/$NAME/turn.jsonl (JSONL of tool calls and results).
Replay it mentally to understand what was done; you do not need to
re-execute destructive operations.

## Preconditions and postconditions
<describe what must be true about the skill's inputs before it runs, and
what must be true about its outputs after. Focus on the contract; do not
prescribe subcommands, flow steps, or argparse surfaces — the worker owns
those decisions.>

## What to do
Use the `crystallize-task-worker` sub-skill to drive the end-to-end build.
The user will interact with you directly in your own chat channel for
outline (Gate 1) and final-artifact (Gate 2) approval.

## Worker sub-skills
The `crystallize-task-worker`, `heal-skill-worker`, and
`update-skill-worker` skills have been pre-installed into your
`.agents/skills/` tree.

## Success criteria
- New skill lives at `.agents/skills/<name>/` with SKILL.md (agentskills.io-
  compliant, `metadata.crystallized: true`) and `scripts/run.py` (PEP 723,
  argparse).
- All hand-crafted scenarios pass when run against `scripts/run.py`.
- User has approved both the outline (Gate 1) and the final artifact (Gate 2).
- Work is committed to the worker's branch (`mngr/crystallize-$NAME`).
TASK_EOF
```

## Step 4: Launch the worker

Follow the `launch-task` skill's conventions for worker lifecycle management
(background waiting, checking results, handling outcomes), with these
crystallize-specific overrides:

- Template: `-t crystallize-worker` (not `-t worker`)
- Task file: the one written in step 3

```bash
mngr create crystallize-$NAME -t crystallize-worker \
    --label workspace=$MINDS_WORKSPACE_NAME \
    --message-file /tmp/task-crystallize-$NAME.md
```

The `crystallize-worker` template (see `.mngr/settings.toml`) inherits from
`worker`, sets `MNGR_AGENT_ROLE=worker` so the Stop hook skips inside the
worker, and runs the bundled-sub-skill installer so the worker's
`.agents/skills/` contains `crystallize-task-worker` et al.

## Step 5: Monitor in background, then merge

The worker runs in its own agent with its own chat channel. The user will
handle gate approvals directly with the worker -- you do not relay
questions. Your remaining job is to be notified when the worker finishes
and then merge the branch.

Start `mngr wait` in the background (using the Bash tool with
`run_in_background: true`) so you can continue working. You will be
notified when it completes -- do not block on it.

```bash
# Run with Bash run_in_background: true
mngr wait crystallize-$NAME DONE STOPPED WAITING --timeout 30m
```

When notified that the wait completed, check the outcome and merge:

```bash
# Check what happened
mngr transcript crystallize-$NAME --role=assistant | tail -n 20

# If successful, merge the branch
git fetch . mngr/crystallize-$NAME:mngr/crystallize-$NAME
git merge --no-ff mngr/crystallize-$NAME
```

If the merge conflicts, resolve manually. If the worker stopped without
producing the expected commit, see `launch-task/references/worker-failure.md`.

On successful merge, close the tracking ticket and optionally destroy the
worker:

```bash
if command -v tk >/dev/null 2>&1 && [ -n "${TICKET_ID:-}" ]; then
    tk close "$TICKET_ID"
fi
# optional: echo "y" | mngr destroy crystallize-$NAME --force
```

## Guidelines

- Never crystallize without explicit user Yes on the pre-gate question.
- Never crystallize a turn whose process you could not explain as a linear
  script. If there is heavy judgement or creativity in the turn, decline.
- The worker owns outline and implementation decisions. Do not second-guess
  the worker's script unless something is clearly wrong.
- Worker failure handling: see `launch-task/references/worker-failure.md`.
