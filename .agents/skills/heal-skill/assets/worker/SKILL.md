---
name: heal-skill-worker
description: Repair a failing skill. Invoke when your task file asks you to heal a specific skill by pointing at its incident transcript.
metadata:
  role: worker-sub-skill
---

# Healing a skill

A crystallized or hand-authored skill misbehaved during a real turn; your
job is to fix it.

## Reporting back to the lead

Follow `.agents/shared/references/worker-reporting.md` for the report-file
procedure and task-file frontmatter schema. Substitute:

- `<RUNTIME_REPORTS_DIR>` → `runtime/heal/reports/`
- `<TASK_FILE_GLOB>` → `runtime/heal/*/task.md`

Valid `name:` values for this worker:

- Gate: `final-artifact` (Stage 6). There is no outline gate for a heal.
- Terminal statuses: `done` (Stage 7), `stuck` (see "If you cannot fix it"
  below).

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
  Use the template in `.agents/shared/references/spec-summary.md`.

## Stage 5: Code review

Run `/autofix` on your commits. Fix anything the reviewer flags.

## Stage 6: Gate 2 -- approval

Write a report with `type: gate`, `name: final-artifact`, and body:

```
Fixed `<skill-name>`:
- Root cause: <one-sentence>
- Change: <one-sentence>
- Scenarios run: <list, all pass>

Approve the fix? (yes / no with notes)
```

Push it and stop, per the reporting procedure above.

## Stage 7: Commit and hand off

Commit on your current branch, then emit a `name: done` terminal report (body
shape per `.agents/shared/references/worker-reporting.md`).

## If you cannot fix it

If the root cause is impossible for you to implement or the right fix would
change the skill's contract in ways the user should decide on, emit a
`name: stuck` terminal report (body shape per
`.agents/shared/references/worker-reporting.md`). Include a recommendation
(e.g. a create-new-skill update or manual investigation).
