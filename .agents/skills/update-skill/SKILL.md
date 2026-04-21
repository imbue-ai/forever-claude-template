---
name: update-skill
description: Extend or refactor a crystallized skill (or split a new one off) when you had to do additional deterministic post-processing beyond what the existing skill did. Invoke at turn-end when reflecting on a successful skill use that left you patching around gaps.
---

# Updating or splitting a skill

Use this skill when an existing skill in `.agents/skills/` ran successfully
but you had to do additional *deterministic* processing to fully satisfy the
user's request. The goal is to fold that processing into the skill (or into a
sibling skill) so it never needs to be redone by hand.

Trigger this via the turn-end reflection in AGENTS.md: "did I do additional
deterministic post-processing the skill could have done itself?" If yes,
invoke update-skill.

## Update vs. create-new: the rubric

The update worker will decide, but you should have a rough expectation:

- **Update-in-place**: the gap is a natural extension of the existing skill
  (e.g. an additional flag, a new output format, an edge case the script
  didn't handle). The skill's identity stays the same.
- **Create-new-skill**: the gap is an orthogonal concern that happens to
  chain onto the first skill's output. A new sibling skill is cleaner than
  stretching the original's surface.

If the post-processing was *non-deterministic* (judgement, creativity,
exploration), it is NOT an update candidate -- it stays with the main agent.

## Conventions

Use `$TARGET` for the skill you are updating (e.g. `migrate-config`). Then:

- Worker agent name: `update-$TARGET`
- Worker branch: `mngr/update-$TARGET`
- Runtime path: `runtime/update/$TARGET/`
- Task file: `/tmp/task-update-$TARGET.md`

## Step 1: Open a tracking ticket

```bash
if command -v tk >/dev/null 2>&1; then
    TICKET_ID=$(tk create "update $TARGET" -t task \
        --acceptance "incident captured; task file written; worker launched; worker DONE; branch merged")
    tk start "$TICKET_ID"
fi
```

## Step 2: Capture the incident transcript

```bash
uv run .agents/skills/crystallize-task/scripts/extract_turn.py \
    --output runtime/update/$TARGET/turn.jsonl
```

The helper auto-discovers the current session transcript from
`$CLAUDE_TRANSCRIPT_PATH` or `$MNGR_CLAUDE_SESSION_ID`.

## Step 3: Write the task file

```bash
cat > /tmp/task-update-$TARGET.md << 'TASK_EOF'
# Task: update the `$TARGET` skill (or split a new one)

## Incident
The turn where `$TARGET` was invoked is at
runtime/update/$TARGET/turn.jsonl.

## What was missing
<describe in 2-5 sentences what the skill did, what additional deterministic
work you had to do by hand, and why folding it in would help future turns.>

## What to do
Use the `update-skill-worker` sub-skill to: replicate the incident,
decide update-in-place vs. new-sibling-skill, run Gate 1 on the outline,
implement, hand-craft 2-3 scenarios, run them, run Gate 2.

## Success criteria
- The additional processing no longer needs to be done manually.
- All scenarios pass.
- User has approved outline (Gate 1) and final artifact (Gate 2).
- Work is committed to the worker's branch (`mngr/update-$TARGET`).
TASK_EOF
```

## Step 4: Launch the worker

```bash
mngr create update-$TARGET -t crystallize-worker \
    --label workspace=$MINDS_WORKSPACE_NAME \
    --message-file /tmp/task-update-$TARGET.md
```

## Step 5: Wait for completion, then merge

The worker runs in its own agent with its own chat channel. The user will
handle both gate approvals (outline + final artifact) directly with the
worker -- you do not relay questions. Wait for DONE (or STOPPED) and then
merge:

```bash
mngr wait update-$TARGET DONE STOPPED &
wait
git fetch . mngr/update-$TARGET:mngr/update-$TARGET
git merge --no-ff mngr/update-$TARGET
```

If the worker stopped without producing the expected commit, see
`launch-task/references/worker-failure.md`.

If the worker decided "create-new-skill", the new skill lands in its own
directory; the old skill is unchanged.

On successful merge, close the tracking ticket:

```bash
if command -v tk >/dev/null 2>&1 && [ -n "${TICKET_ID:-}" ]; then
    tk close "$TICKET_ID"
fi
```

## Gotchas

- If the target is a built-in skill from the upstream template (e.g.
  `launch-task`, `update-self`), updating it causes local drift from
  upstream. Reconcile later via `update-self` (pull) or
  `submit-upstream-changes` (push).
- Update is non-blocking -- the user's original request is already
  delivered; the update worker just produces a quieter follow-up commit.
