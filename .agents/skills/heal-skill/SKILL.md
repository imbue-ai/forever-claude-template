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
- Task file: `runtime/heal/$TARGET/task.md` (the Step 3 push syncs it to
  the worker)

## Step 1: Open a tracking ticket

```bash
TICKET_ID=$(tk create "heal $TARGET" -t bug \
    --acceptance "task file written; worker launched; worker DONE; branch merged")
tk start "$TICKET_ID"
```

## Step 2: Write the task file

The worker will explore your transcript via `mngr transcript` to find
the incident. Your job here is to write a task body that describes the
failure and anchors the worker's search with verbatim quotes (the
user's request, the failing command or error message, any tool output
that exposed the misbehavior). Without anchors the worker will scan the
wrong region of your transcript.

The task file's YAML frontmatter follows the schema in
`.agents/shared/references/worker-reporting.md`.

```bash
mkdir -p runtime/heal/$TARGET
{
cat << FRONTMATTER_EOF
---
lead_agent: $MNGR_AGENT_NAME
lead_report_dir: runtime/heal/$TARGET/reports/
---
FRONTMATTER_EOF
cat << BODY_EOF

# Task: heal the \`$TARGET\` skill

## Incident summary
<2-5 sentences: what the user asked for, how \`$TARGET\` was invoked,
how it failed, what you did to work around it.>

## Anchors (verbatim quotes)
The worker will use these to locate the incident in your transcript via
\`mngr transcript\`. Include:
- The user's request that invoked \`$TARGET\` (verbatim).
- The failing output, exception, or wrong result (verbatim).
- Any clarifying quote from the user about expected behavior.
<paste quotes here, one per bullet.>

## How to read the transcript
Use \`mngr transcript <lead_agent>\` (with \`--role user --role assistant\`
to strip tool noise, or \`--tail N\` to scope in) to find the turns above.
The heal-skill invocation is the *most recent* turn; the incident is
*prior* to that invocation.

## What the fixed skill must do
<state the contract the healed skill must honor — what input shapes
should work, what outputs are correct. Describe only what success
looks like; the incident itself is captured above.>

## What to do
Use the \`heal-skill-worker\` sub-skill to replicate the problem, find
the root cause, apply a fix to the relevant part of
\`.agents/skills/$TARGET/\` (SKILL.md prose, scripts, or both), re-run
fresh 2-3 scenarios against the fixed skill, and push through the
final-artifact gate (user approval of the fix). A heal has only that
single gate -- no outline gate.

When you reach the final-artifact gate or a terminal status, write a
report file and push it to the lead per the sub-skill's reporting
protocol; the destination is given by \`lead_agent\` /
\`lead_report_dir\` in frontmatter.

## Success criteria
- The incident reproduces against the current skill before the fix.
- The fix addresses the root cause (not a symptom workaround).
- The fresh scenarios pass after the fix.
- The user approves the final artifact (via a pushed \`final-artifact\` gate report).
- Work is committed to your branch.
BODY_EOF
} > runtime/heal/$TARGET/task.md
```

Fill in the `## Incident summary` and `## Anchors` sections with real
content drawn from your conversation -- do not leave the placeholders.

## Step 3: Launch the worker

The shared `launch-task` dispatcher runs `mngr create`, pushes the
runtime dir (task file) into the worker's worktree, and sends the task
as a follow-up message so the worker sees the runtime dir first. The
`crystallize-worker` template pre-installs `heal-skill-worker`
alongside the other worker sub-skills.

```bash
uv run .agents/skills/launch-task/scripts/dispatch.py \
    --name heal-$TARGET \
    --template crystallize-worker \
    --runtime-dir runtime/heal/$TARGET/ \
    --task-file runtime/heal/$TARGET/task.md
```

## Step 4: Proxy the final-artifact gate, then merge

Follow `.agents/shared/references/lead-proxy.md` for polling, gate
decisions, the "do not interrupt more recent user work" rule, and
terminal-status handling.

Flow-specific substitutions:

- Worker name: `heal-$TARGET`
- Branch: `mngr/heal-$TARGET`
- Poll path: `runtime/heal/$TARGET/reports/report.md`
- Consumed path: `runtime/heal/$TARGET/reports/consumed/`
- The only user-approval gate is `type: gate, name: final-artifact`.
  A heal has no outline gate.
- Terminal statuses: `type: status, name: done` (merge);
  `type: status, name: stuck` (failure-handling flow).

On successful merge, close the tracking ticket:

```bash
tk close "$TICKET_ID"
```

## Gotchas

- If the target is a built-in skill from the upstream template (e.g.
  `launch-task`, `update-self`), healing it causes local drift from
  upstream. Reconcile later via `update-self` (pull) or
  `submit-upstream-changes` (push).
- Heal is non-blocking. The user's original request is already delivered;
  the heal worker just produces a quieter follow-up commit.
