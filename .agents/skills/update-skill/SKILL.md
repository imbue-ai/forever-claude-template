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

## Step 1: Capture the incident transcript

```bash
uv run .agents/skills/crystallize-task/scripts/extract_turn.py \
    --transcript "$CLAUDE_TRANSCRIPT_PATH" \
    --output runtime/update/<skill-name>/turn.jsonl
```

The worker will get the bundled `update-crystallized-skill` sub-skill
installed into its own `.agents/skills/` at provision time; no manual
staging from your side is needed.

## Step 2: Write the task file

```bash
cat > /tmp/task-update-<skill-name>.md << 'TASK_EOF'
# Task: update the `<skill-name>` skill (or split a new one)

## Incident
The turn where `<skill-name>` was invoked is at
runtime/update/<skill-name>/turn.jsonl.

## What was missing
<describe in 2-5 sentences what the skill did, what additional deterministic
work you had to do by hand, and why folding it in would help future turns.>

## What to do
Use the `update-crystallized-skill` sub-skill to: replicate the incident,
decide update-in-place vs. new-sibling-skill, run Gate 1 on the outline,
implement, hand-craft 2-3 scenarios, run them, run Gate 2.

## Success criteria
- The additional processing no longer needs to be done manually.
- All scenarios pass.
- User has approved outline (Gate 1) and final artifact (Gate 2).
- Work is committed to the `mngr/update-<skill-name>` branch.
TASK_EOF
```

## Step 3: Launch the worker

```bash
mngr create update-<skill-name> -t crystallize-worker \
    --label workspace=$MINDS_WORKSPACE_NAME \
    --message-file /tmp/task-update-<skill-name>.md
mngr wait update-<skill-name> DONE STOPPED WAITING &
```

## Step 4: Relay the gate questions

Update has two gates (outline + final artifact). Relay each via
`send-telegram-message`; forward each reply via `mngr message update-<skill-name>`.

## Step 5: Merge on approval

```bash
git fetch . mngr/update-<skill-name>:mngr/update-<skill-name> 2>/dev/null || true
git merge --no-ff mngr/update-<skill-name>
```

## Caveats

- Same drift caveats as `heal-skill` if the target is a built-in skill.
- Update is non-blocking -- the user's original request is already
  delivered.
- If the worker decides "create-new-skill", the new skill lands in its own
  directory; the old skill is unchanged.
