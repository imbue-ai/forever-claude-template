---
name: crystallize-task
description: Turn the turn that just finished into a reusable deterministic skill. Invoke when the Stop hook has just reminded you that the turn used many non-read tool calls AND the work was a cohesive single unit likely to recur with a mostly deterministic process.
---

# Crystallizing a task into a skill

Use this skill to promote ad-hoc work from the turn that just finished into a
reusable skill consisting of a PEP 723 `scripts/run.py` and a companion
`SKILL.md`, both [agentskills.io](https://agentskills.io/specification)-compliant.

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

## Step 1: Confirm with the user

Send a single-line pre-gate question through the deployment's chat channel
(see `send-telegram-message`). Example:

> "I just did X and Y. Worth crystallizing into a reusable skill? (yes/no)"

Wait for the user's reply before proceeding. If they say no, stop here.

## Step 2: Extract the just-finished turn

The worker needs the raw transcript of the turn to replay the work. Extract
it from the live session JSONL via the helper script:

```bash
uv run .agents/skills/crystallize-task/scripts/extract_turn.py \
    --transcript "$CLAUDE_TRANSCRIPT_PATH" \
    --output runtime/crystallize/<task-name>/turn.jsonl
```

`<task-name>` is a short kebab-case slug you pick for the crystallize worker
(not the final skill name -- the worker proposes that during its Gate 1
outline). `$CLAUDE_TRANSCRIPT_PATH` is provided by Claude Code in the
session's environment; if not set, discover the current transcript under
`~/.claude/projects/<slug>/`.

## Step 3: Write the task file

The worker will automatically install the bundled sub-skills
(`build-crystallized-skill`, `heal-crystallized-skill`,
`update-crystallized-skill`) into its own `.agents/skills/` at provision
time -- no action needed from you.

Then write the task file:

```bash
cat > /tmp/task-crystallize-<task-name>.md << 'TASK_EOF'
# Task: crystallize the just-finished work into a reusable skill

## Transcript
The turn you need to crystallize is at
runtime/crystallize/<task-name>/turn.jsonl (JSONL of tool calls and results).
Replay it mentally to understand what was done; you do not need to re-execute
destructive operations.

## What to do
Use the `build-crystallized-skill` sub-skill to drive the end-to-end build:
replicate -> propose outline -> user Gate 1 -> implement script + SKILL.md ->
hand-craft 2-3 scenarios -> run them -> user Gate 2 -> commit.

## Worker sub-skills
The `build-crystallized-skill`, `heal-crystallized-skill`, and
`update-crystallized-skill` skills have been pre-installed into your
`.agents/skills/` tree.

## Success criteria
- New skill lives at `.agents/skills/<name>/` with SKILL.md (agentskills.io-
  compliant, `metadata.crystallized: true`) and `scripts/run.py` (PEP 723, argparse).
- All hand-crafted scenarios pass when run against `scripts/run.py`.
- User has approved both the outline (Gate 1) and the final artifact (Gate 2).
- Work is committed to the `mngr/<task-name>` branch.
TASK_EOF
```

## Step 4: Launch the worker

```bash
mngr create crystallize-<task-name> -t crystallize-worker \
    --label workspace=$MINDS_WORKSPACE_NAME \
    --message-file /tmp/task-crystallize-<task-name>.md
mngr wait crystallize-<task-name> DONE STOPPED WAITING &
```

The `crystallize-worker` template (see `.mngr/settings.toml`) inherits from
`worker`, sets `MNGR_AGENT_ROLE=worker` so the Stop hook skips inside the
worker, and runs the bundled-sub-skill installer as a provision step so the
worker's `.agents/skills/` contains `build-crystallized-skill` et al.

## Step 5: Relay gate questions

The worker will end its turn twice with a user-facing question (outline, then
final artifact). When this happens, the worker will be in WAITING. Relay the
question to the user verbatim via `send-telegram-message`; when the user
replies, forward their answer via `mngr message crystallize-<task-name> -m
"<reply>"`.

## Step 6: Merge on approval

Once the worker finishes after Gate 2 approval, merge its branch into your
working branch so the new skill is discoverable:

```bash
git fetch . mngr/<task-name>:mngr/<task-name> 2>/dev/null || true
git merge --no-ff mngr/<task-name>
```

If the merge conflicts, resolve manually. After merge, optionally
`mngr destroy crystallize-<task-name>`.

## Guidelines

- Never crystallize without explicit user Yes on the pre-gate question.
- Never crystallize a turn whose process you could not explain as a linear
  script. If there is heavy judgement or creativity in the turn, decline.
- The worker owns outline and implementation decisions; your job is to supply
  context, relay gates, and merge. Do not second-guess the worker's script
  unless something is clearly wrong.
- If the worker fails or produces something wrong, do NOT retry here. Report
  the failure to the user and move on; they can re-invoke later with more
  context.
