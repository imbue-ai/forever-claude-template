---
name: heal-skill
description: Fix a crystallized or hand-authored skill that errored or delivered a wrong result. Invoke at turn-end when a Skill tool invocation failed and you had to work around it to satisfy the user's request.
---

# Healing a broken skill

Use this skill when an existing skill in `.agents/skills/` should have
delivered the correct result but did not. Typical triggers:

- A script under `scripts/` raised an exception or returned a non-zero
  exit.
- The skill's scripts ran to completion but produced output that did not
  satisfy the user's request, forcing you to patch around it.
- The SKILL.md prose instructions were ambiguous, incomplete, or wrong,
  causing you (as the agent using the skill) to take the wrong action on
  a realistic input.
- A missing step or capability in the skill -- script or prose --
  prevented it from handling a realistic input shape.

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
    --nth 1 \
    --output runtime/heal/$TARGET/turn.jsonl
```

The helper auto-discovers the current session transcript from
`$CLAUDE_TRANSCRIPT_PATH` or `$MNGR_CLAUDE_SESSION_ID`.

`--nth 1` selects the *previous* human turn -- the one where the skill
misbehaved. `--nth 0` (the default) would select the current heal-skill
invocation turn itself, which is not the incident you need to replay.

If counting turns does not line up cleanly (e.g. sub-agent interleaving),
use `--start-marker TEXT` and optionally `--end-marker TEXT` to slice by
matching text content instead.

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
the root cause, apply a fix to the relevant part of
`.agents/skills/$TARGET/` (SKILL.md prose, scripts, or both), re-run
fresh 2-3 scenarios against the fixed skill, and push through Gate 2
(user approval of the final artifact). There is no outline gate for a
heal.

Emit gate questions and status updates inline in your response, using
the headers the sub-skill defines (e.g. `## GATE: final-artifact`,
`## STATUS: done`). Do NOT call `send-user-message` or any other
channel skill for gates -- the user reads your response inline.

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

The worker runs in a separate git worktree, so it cannot see files
under `runtime/` (which is gitignored). Push the incident transcript
into the worker's working directory so the task message's path resolves
there. Run this immediately after `mngr create`:

```bash
mngr push heal-$TARGET:runtime/heal/$TARGET runtime/heal/$TARGET
```

See `.agents/skills/launch-task/SKILL.md` (Worktree isolation section)
for background.

## Step 5: Proxy Gate 2, then merge

The user sees your chat, not the worker's. The user can view the
worker's chat if they want to, but they are not required to -- so you
drive the worker to completion by proxying its Gate 2 and any mid-flow
questions.

Follow the same proxy flow as
`.agents/skills/crystallize-task/SKILL.md` step 5 (subsections 5a-5f),
with these substitutions:

- Worker name: `heal-$TARGET`
- Branch: `mngr/heal-$TARGET`
- Transcript capture path: `/tmp/worker-heal-$TARGET-transcript.txt`
- The only user-approval gate is `## GATE: final-artifact` (Gate 2).
  There is no outline gate for a heal.
- Terminal markers: `## STATUS: done` (merge), `## STATUS: stuck`
  (failure-handling flow).

As a reminder: do not interrupt more recent user work to handle a
worker notification. Answer implementation-detail questions yourself;
escalate Gate 2 approval to the user.

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
