---
name: heal-crystallized-skill
description: Worker sub-skill that repairs a failing skill. Invoke at the start of a heal-worker session launched by the main agent's heal-skill skill.
metadata:
  role: worker-sub-skill
---

# Healing a skill (worker flow)

You were launched by the main agent's `heal-skill` skill. A crystallized or
hand-authored skill misbehaved during a real turn; your job is to fix it.
There is no outline gate -- heal is a tight diagnose-fix-verify loop with
only a final Gate 2.

## Stage 1: Replicate

1. Read the task file to learn which skill failed and how.
2. Read the incident transcript (`runtime/heal/<skill-name>/turn.jsonl`).
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
- Do not add test-only exports or TODO comments.
- Fail loudly on unexpected input rather than silently swallowing.

## Stage 4: Re-run 2-3 fresh scenarios

- Include the incident itself as one scenario.
- Include 1-2 additional scenarios that exercise neighbouring code paths
  to make sure the fix didn't regress anything.
- Scenarios are ephemeral (run in your transcript, not saved to disk).

## Stage 5: Code-guardian review

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

Commit on your `mngr/heal-<skill-name>` branch. Confirm the branch name in
your final response so the main agent knows what to merge.

## If you cannot fix it

If the root cause is out of scope (e.g. upstream API change, environmental
problem) or the right fix would change the skill's contract in ways the
user should decide on, end your turn with:

> "I could not heal `<skill-name>` because: <reason>. Recommend:
> <next step, e.g. invoke update-skill or manual investigation>."

and stop.
