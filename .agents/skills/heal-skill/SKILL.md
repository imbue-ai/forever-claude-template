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

Do NOT use heal for a drift between what the skill *does* and what the user
is *now asking it to do* -- that is an `update-skill` situation.

## When NOT to heal

- The skill worked fine; the user's request was genuinely out of its scope.
  (Consider `update-skill` instead.)
- The failure was one-off and transient (network hiccup, rate limit).
- You are unsure why it failed. Finish the user's request first; gather
  evidence; then decide if heal applies.

Heal is a turn-end action -- do not interrupt in-flight work to invoke it.

## Step 1: Capture the incident transcript

The worker needs a replay of the turn where the skill misbehaved. Use the
same transcript-extraction helper as `crystallize-task`:

```bash
uv run .agents/skills/crystallize-task/scripts/extract_turn.py \
    --transcript "$CLAUDE_TRANSCRIPT_PATH" \
    --output runtime/heal/<skill-name>/turn.jsonl
```

The worker will get the bundled `heal-crystallized-skill` sub-skill
installed into its own `.agents/skills/` at provision time; no manual
staging from your side is needed.

## Step 2: Write the task file

```bash
cat > /tmp/task-heal-<skill-name>.md << 'TASK_EOF'
# Task: heal the `<skill-name>` skill

## Incident
The turn where `<skill-name>` misbehaved is at
runtime/heal/<skill-name>/turn.jsonl.

## What went wrong
<summarize in 2-5 sentences: what was invoked, what happened, what the user
actually needed. Quote the key error/output if short.>

## What to do
Use the `heal-crystallized-skill` sub-skill to replicate the problem, find
the root cause, apply a fix to `.agents/skills/<skill-name>/scripts/run.py`
and/or `.agents/skills/<skill-name>/SKILL.md`, re-run fresh 2-3 scenarios
against the fixed script, and push through Gate 2 (user approval of the
final artifact). There is no outline gate for a heal.

## Success criteria
- The incident reproduces against the current skill before the fix.
- The fix addresses the root cause (not a symptom workaround).
- The fresh scenarios pass after the fix.
- The user approves the final artifact (Gate 2).
- Work is committed to the `mngr/heal-<skill-name>` branch.
TASK_EOF
```

## Step 3: Launch the worker

```bash
mngr create heal-<skill-name> -t crystallize-worker \
    --label workspace=$MINDS_WORKSPACE_NAME \
    --message-file /tmp/task-heal-<skill-name>.md
mngr wait heal-<skill-name> DONE STOPPED WAITING &
```

`crystallize-worker` is the right template here too -- it pre-installs the
heal sub-skill (alongside build/update) into the worker's worktree.

## Step 4: Relay the Gate 2 question

When the worker ends its turn with "approve the fix?", relay via
`send-telegram-message` and forward the reply back through
`mngr message heal-<skill-name>`. See the `crystallize-task` skill for the
full relay pattern.

## Step 5: Merge on approval

```bash
git fetch . mngr/heal-<skill-name>:mngr/heal-<skill-name> 2>/dev/null || true
git merge --no-ff mngr/heal-<skill-name>
```

## Caveats

- Built-in skills from the upstream template (`launch-task`, `update-self`,
  etc.) are eligible for heal, but doing so causes local drift from
  upstream. Reconcile manually via `update-self` (pull) or
  `submit-upstream-changes` (push) later.
- Heal is non-blocking. The user's original request is already delivered;
  the heal worker just produces a quieter follow-up commit.
