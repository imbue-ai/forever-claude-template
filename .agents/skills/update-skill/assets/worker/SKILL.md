---
name: update-skill-worker
description: Extend an existing skill, split off a new sibling skill, or verify a live-committed skill change. Invoke when your task file asks you to fold additional processing into an existing skill (absorb flow) or to verify a skill change that was already committed live (verify flow).
metadata:
  role: worker-sub-skill
---

# Updating (or verifying) a skill

An existing skill needs a change. There are two flavors:

- **absorb flow.** A skill ran but additional *repeatable* work had to be
  done by hand. You replicate the incident, decide update-in-place vs.
  sibling-split, propose the design at Gate 1, implement, run scenarios,
  and present Gate 2.
- **verify flow.** The main agent and the user already discussed and
  committed the change. You skip design gates, read the committed change,
  run scenarios, run `/autofix`, and present Gate 2 with verification
  findings.

## Stage 0: Detect flow

Read your task file. Look for a top-level `## Flow` section with
`FLOW: absorb` or `FLOW: verify`. The marker is required -- fail loudly
if absent or unrecognized; each task-file writer must emit it.

Then dispatch:

- **absorb:** follow `references/worker-absorb.md` for Stages 1-9.
- **verify:** follow `references/worker-verify.md` for its stages
  (design gates skipped; verification-only).

The rest of this file holds content that is shared across flows:
principles, the reporting-back procedure, and validation/scenario
pointers.

## Principles

"Repeatable" covers both script-shaped extensions (extra flag, new output
format) and prose-shaped extensions (an additional judgement step with a
stable recipe). Both land inside a skill: scripts under `scripts/`,
judgement steps as SKILL.md prose.

**Reliability is the floor; simplicity is the target.** Default to a
single entry point and one flow. Add surface only when a specific
invariant demands it.

Consult `.agents/skills/crystallize-task-worker/references/spec-summary.md`
for the layout, frontmatter, validation helpers, and scenario template.

## Update-in-place vs. create-new-skill (absorb flow only)

See `.agents/shared/references/update-vs-create-new.md` for the full
rubric. Default to update-in-place; only split when the gap has a concrete
standalone use case. In the verify flow the decision has already been made
by the committed change.

## Reporting back to the lead

Follow `.agents/shared/references/worker-reporting.md` for the report-file
procedure and task-file frontmatter schema. Substitute:

- `<RUNTIME_REPORTS_DIR>` → `runtime/update/reports/`
- `<TASK_FILE_GLOB>` → `runtime/update/*/task.md`

Which gates apply:

- **absorb:** Gate 1 (`outline-approval`), Gate 2 (`final-artifact`).
- **verify:** Gate 2 only (`final-artifact`).

Terminal statuses (both flows): `done`, `stuck`, `no-update-needed`.

## Validation (both flows)

Before emitting Gate 2, validate the target skill's layout:

```bash
uv run .agents/shared/scripts/validate_skill.py .agents/skills/<name>
```

It must print `ok` and exit 0.

## Scenarios (both flows)

At least one scenario must exercise the new behaviour (the absorbed
incident in the absorb flow; the changed path in the verify flow). Others
should exercise neighbouring or edge paths.

Scenarios are ephemeral -- they live in the transcript for reproducibility,
not on disk. Use the template in
`.agents/skills/crystallize-task-worker/references/spec-summary.md`.

## If you decide no change is needed

Applies to both flows. If the right answer turns out to be "leave the
skill alone", write a terminal report with `type: status`,
`name: no-update-needed`, and body:

```
No update needed. Reason: <one-sentence>.
```

Push it and stop. Do not commit a null change.
