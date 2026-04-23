---
name: heal-skill-worker
description: Repair a failing skill. Invoke when your task file asks you to heal a specific skill by pointing at its incident transcript.
metadata:
  role: worker-sub-skill
---

# Healing a skill

A crystallized or hand-authored skill misbehaved during a real turn; your
job is to fix it.

## Stage 1: Replicate

1. Read the task file to learn which skill failed and how.
2. Read the incident transcript the task file points at.
3. Read the skill's current `SKILL.md` and any scripts under `<skill_directory>/scripts/`.
   A skill may be pure prose (no scripts), in which case "replicate" means
   tracing the SKILL.md instructions against the incident inputs to see
   where the recipe led astray.
4. Reproduce the failure. If the failure depends on external state you
   cannot recreate, construct a minimal synthetic input that exercises the
   same code path (or, for a prose-only skill, the same branch of the
   recipe).

## Stage 2: Diagnose the root cause

- Trace the actual runtime code path -- do NOT guess from surface
  observations.
- Identify the root cause and briefly write it down in your transcript
  (one or two sentences) before touching code.
- If you cannot identify a root cause confidently, end the turn with
  "could not diagnose" and a description of what you observed. Don't
  apply a speculative fix.

## Stage 3: Apply the fix

- Edit the relevant part of the skill to address the root cause. That
  can be scripts under `scripts/`, `SKILL.md` prose, or both. If the
  root cause was an ambiguous or wrong prose instruction, the fix is a
  SKILL.md edit even if the skill has scripts.
- Keep the fix minimal. Don't refactor unrelated code or prose.
- A heal is a minimal fix. If the fix is growing enough to feel like a
  redesign (new subcommands, new flows, expanded contract, new dependencies), stop and
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

End your turn with a response that begins with this exact header on its
own line, followed by the summary prose:

```
## GATE: final-artifact

Fixed `<skill-name>`:
- Root cause: <one-sentence>
- Change: <one-sentence>
- Scenarios run: <list, all pass>

Approve the fix? (yes / no with notes)
```

Emit this inline -- do not use `send-user-message` or any other channel
skill.

Wait for the user's reply.

## Stage 7: Commit and hand off

Commit on your current branch. End your final response with this exact
header on its own line, followed by the hand-off summary:

```
## STATUS: done

Committed on branch `<branch-name>`. Ready to merge.
```

## If you cannot fix it

If the root cause is out of scope (e.g. upstream API change,
environmental problem) or the right fix would change the skill's
contract in ways the user should decide on, end your turn with:

```
## STATUS: stuck

I could not heal `<skill-name>` because: <reason>. Recommend: <next
step, e.g. a create-new-skill update or manual investigation>.
```

and stop.