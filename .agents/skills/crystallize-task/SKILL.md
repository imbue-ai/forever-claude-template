---
name: crystallize-task
description: Turn the turn that just finished into a reusable deterministic skill. Invoke when the Stop hook has just reminded you that the turn used many non-read tool calls AND the work was a cohesive single unit likely to recur with a mostly deterministic process.
---

# Crystallizing a task into a skill

Use this skill to promote ad-hoc work from the turn that just finished into a
reusable skill consisting of a PEP 723 `scripts/run.py` and a companion
`SKILL.md`, both [agentskills.io](https://agentskills.io/specification)-compliant.
You dispatch the actual build to a sub-agent; your role is to package context,
launch, and merge.

## When to invoke

The Stop hook emits a reminder whenever the turn used at least five non-read
tool calls. That threshold is deliberately dumb -- you supply the judgement.
Only crystallize when ALL of these hold:

1. The work was a single cohesive unit (not a mixed-bag turn that happened to
   touch many files).
2. The underlying process is mostly deterministic -- it could be expressed as
   a script with clear inputs and outputs.
3. You expect this task to recur, either verbatim or with minor input changes.

If any of the above is false, just send a short acknowledgement to the user
and move on. A pure-research turn, a one-off incident response, or creative
writing should never be crystallized.

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

Send a one-line pre-gate question through the deployment's chat channel
(`send-telegram-message`):

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
    --output runtime/crystallize/$NAME/turn.jsonl
```

The helper auto-discovers the current session transcript via
`$CLAUDE_TRANSCRIPT_PATH` (set inside hooks) or `$MNGR_CLAUDE_SESSION_ID`.
Do not pass `--transcript` unless you have a specific file to replay.

## Step 3: Write the task file

```bash
cat > /tmp/task-crystallize-$NAME.md << 'TASK_EOF'
# Task: crystallize the just-finished work into a reusable skill

## Transcript
The turn you need to crystallize is at
runtime/crystallize/$NAME/turn.jsonl (JSONL of tool calls and results).
Replay it mentally to understand what was done; you do not need to
re-execute destructive operations.

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

```bash
mngr create crystallize-$NAME -t crystallize-worker \
    --label workspace=$MINDS_WORKSPACE_NAME \
    --message-file /tmp/task-crystallize-$NAME.md
```

The `crystallize-worker` template (see `.mngr/settings.toml`) inherits from
`worker`, sets `MNGR_AGENT_ROLE=worker` so the Stop hook skips inside the
worker, and runs the bundled-sub-skill installer so the worker's
`.agents/skills/` contains `crystallize-task-worker` et al.

## Step 5: Wait for completion, then merge

The worker runs in its own agent with its own chat channel. The user will
handle gate approvals directly with the worker -- you do not relay
questions. Your remaining job is to wait for the worker to reach DONE (or
STOPPED) and then merge the branch so the new skill is discoverable.

```bash
mngr wait crystallize-$NAME DONE STOPPED &
wait
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
# optional: mngr destroy crystallize-$NAME
```

## Guidelines

- Never crystallize without explicit user Yes on the pre-gate question.
- Never crystallize a turn whose process you could not explain as a linear
  script. If there is heavy judgement or creativity in the turn, decline.
- The worker owns outline and implementation decisions. Do not second-guess
  the worker's script unless something is clearly wrong.
- Worker failure handling: see `launch-task/references/worker-failure.md`.
