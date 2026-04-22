---
name: heal-skill-worker
description: Repair a failing skill. Invoke when your task file asks you to heal a specific skill by pointing at its incident transcript.
metadata:
  role: worker-sub-skill
---

# Healing a skill

A crystallized or hand-authored skill misbehaved during a real turn; your
job is to fix it.

**Principle.** Reliability is the floor; simplicity is the target. Default to
a single entry point and one flow. Add surface only when a specific invariant
demands it.

There is no outline gate for a heal: the diagnosis is already in the task
file, and the fix is small. Inserting a gate would just stall the tight
diagnose-fix-verify loop without giving the user useful new information.
Gate 2 is kept as a final safety check on the actual change.

## Stage 1: Replicate

1. Read the task file to learn which skill failed and how.
2. Read the incident transcript the task file points at.
3. Read the skill's current `SKILL.md` and `scripts/run.py`.
4. Reproduce the failure. If the failure depends on external state you
   cannot recreate, construct a minimal synthetic input that exercises the
   same code path.

## Stage 2: Diagnose the root cause

- Trace the actual runtime code path -- do NOT guess from surface
  observations.
- Identify the root cause and briefly write it down in your transcript
  (one or two sentences) before touching code.
- If you cannot identify a root cause confidently, end the turn with
  "could not diagnose" and a description of what you observed. Don't
  apply a speculative fix.

## Stage 3: Apply the fix

- Edit `scripts/run.py` and/or `SKILL.md` to address the root cause.
- Keep the fix minimal. Don't refactor unrelated code.
- A heal is a minimal fix. If the fix is growing enough to feel like a
  redesign (new subcommands, new flows, expanded contract), stop and
  escalate to `update-skill` instead.
- Do not add test-only exports or TODO comments.
- Fail loudly on unexpected input rather than silently swallowing.

## Stage 4: Re-run 2-3 fresh scenarios

- Include the incident itself as one scenario.
- Include 1-2 additional scenarios that exercise neighbouring code paths
  to make sure the fix didn't regress anything.
- Scenarios are ephemeral (run in your transcript, not saved to disk).
  Use the template in
  `../crystallize-task-worker/references/spec-summary.md`.

## Stage 5: Code review

Run `/autofix` on your commits. Fix anything the reviewer flags.

## Stage 6: Gate 2 -- approval

End your turn with:

> "Fixed `<skill-name>`:
> - Root cause: <one-sentence>
> - Change: <one-sentence>
> - Scenarios run: <list, all pass>
>
> Approve the fix? (yes / no with notes)"

Wait for the user's reply.

## Stage 7: Commit and hand off

Commit on your current branch. In your final response, state the branch
name so the caller knows what to merge.

## If you cannot fix it

If the root cause is out of scope (e.g. upstream API change, environmental
problem) or the right fix would change the skill's contract in ways the
user should decide on, end your turn with:

> "I could not heal `<skill-name>` because: <reason>. Recommend:
> <next step, e.g. a create-new-skill update or manual investigation>."

and stop.

## Gotchas

- You run with `MNGR_AGENT_ROLE=worker` in the environment. The
  crystallization Stop hook detects this and stays silent, so you will NOT
  see a crystallization reminder after a heavy sub-turn. Don't try to
  recursively crystallize work you do while healing this skill.
