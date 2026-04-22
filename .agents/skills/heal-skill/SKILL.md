---
name: heal-skill
description: Fix a crystallized or hand-authored skill that errored or delivered a wrong result. Invoke at turn-end when a Skill tool invocation failed and you had to work around it to satisfy the user's request.
---

# Healing a broken skill

Use this skill when an existing skill in `.agents/skills/` should have
delivered the correct result but did not. Typical triggers:

- `scripts/run.py` raised an exception or returned a non-zero exit.
- The script ran to completion but produced output that did not satisfy the
  user's request, forcing you to patch around it.
- A missing capability in the script prevented it from handling a realistic
  input shape.

**Principle.** Reliability is the floor; simplicity is the target. Default to
a single entry point and one flow. Add surface only when a specific invariant
demands it.

Do NOT use heal for a drift between what the skill *does* and what the user
is *now asking it to do* -- that is an `update-skill` situation.

## When NOT to heal

- The skill worked fine; the user's request was genuinely out of its scope.
  (Consider `update-skill` instead.)
- The failure was one-off and transient (network hiccup, rate limit).
- You are unsure why it failed. Finish the user's request first; gather
  evidence; then decide if heal applies.

Heal is a turn-end action -- do not interrupt in-flight work to invoke it.

## Conventions

Use `$TARGET` for the skill you are healing (e.g. `migrate-config`). Then:

- Worker agent name: `heal-$TARGET`
- Worker branch: `mngr/heal-$TARGET`
- Runtime path: `runtime/heal/$TARGET/`
- Task file: `/tmp/task-heal-$TARGET.md`

## Step 1: Open a tracking ticket

```bash
if command -v tk >/dev/null 2>&1; then
    TICKET_ID=$(tk create "heal $TARGET" -t bug \
        --acceptance "incident captured; task file written; worker launched; worker DONE; branch merged")
    tk start "$TICKET_ID"
fi
```

## Step 2: Capture the incident transcript

```bash
uv run .agents/skills/crystallize-task/scripts/extract_turn.py \
    --output runtime/heal/$TARGET/turn.jsonl
```

The helper auto-discovers the current session transcript from
`$CLAUDE_TRANSCRIPT_PATH` or `$MNGR_CLAUDE_SESSION_ID`.

## Step 3: Write the task file

```bash
cat > /tmp/task-heal-$TARGET.md << 'TASK_EOF'
# Task: heal the `$TARGET` skill

## Incident
The turn where `$TARGET` misbehaved is at
runtime/heal/$TARGET/turn.jsonl.

## What the fixed skill must do
<state the contract the healed skill must honor — what input shapes should
work, what outputs are correct. Read the incident transcript for how it
failed; here, describe only what success looks like.>

## What to do
Use the `heal-skill-worker` sub-skill to replicate the problem, find
the root cause, apply a fix to `.agents/skills/$TARGET/scripts/run.py`
and/or `.agents/skills/$TARGET/SKILL.md`, re-run fresh 2-3 scenarios
against the fixed script, and push through Gate 2 (user approval of the
final artifact). There is no outline gate for a heal.

## Success criteria
- The incident reproduces against the current skill before the fix.
- The fix addresses the root cause (not a symptom workaround).
- The fresh scenarios pass after the fix.
- The user approves the final artifact (Gate 2).
- Work is committed to the worker's branch (`mngr/heal-$TARGET`).
TASK_EOF
```

## Step 4: Launch the worker

```bash
mngr create heal-$TARGET -t crystallize-worker \
    --label workspace=$MINDS_WORKSPACE_NAME \
    --message-file /tmp/task-heal-$TARGET.md
```

The `crystallize-worker` template pre-installs `heal-skill-worker`
alongside the other worker sub-skills.

## Step 5: Wait for completion, then merge

The worker runs in its own agent with its own chat channel. The user will
handle the Gate 2 approval directly with the worker -- you do not relay
questions. Wait for DONE (or STOPPED) and then merge:

```bash
mngr wait heal-$TARGET DONE STOPPED &
wait
git fetch . mngr/heal-$TARGET:mngr/heal-$TARGET
git merge --no-ff mngr/heal-$TARGET
```

If the worker stopped without producing the expected commit, see
`launch-task/references/worker-failure.md`.

On successful merge, close the tracking ticket:

```bash
if command -v tk >/dev/null 2>&1 && [ -n "${TICKET_ID:-}" ]; then
    tk close "$TICKET_ID"
fi
```

## Gotchas

- If the target is a built-in skill from the upstream template (e.g.
  `launch-task`, `update-self`), healing it causes local drift from
  upstream. Reconcile later via `update-self` (pull) or
  `submit-upstream-changes` (push).
- Heal is non-blocking. The user's original request is already delivered;
  the heal worker just produces a quieter follow-up commit.
