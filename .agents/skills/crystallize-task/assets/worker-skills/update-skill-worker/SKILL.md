---
name: update-skill-worker
description: Extend an existing skill, split off a new sibling skill, or verify a live-committed skill change. Invoke when your task file asks you to fold additional processing into an existing skill (Mode A) or to verify a skill change that was already committed live (Mode B).
metadata:
  role: worker-sub-skill
---

# Updating (or verifying) a skill

An existing skill needs a change. There are two flavors:

- **Mode A (incident absorption) -- default.** A skill ran but
  additional *repeatable* work had to be done by hand. You replicate
  the incident, decide update-in-place vs. sibling-split, propose the
  design at Gate 1, implement, run scenarios, and present Gate 2.
- **Mode B (live collaborative update).** The main agent and the user
  already discussed and committed the change. You skip design gates,
  read the committed change, run scenarios, run `/autofix`, and
  present Gate 2 with verification findings.

## Stage 0: Detect mode

Read your task file. Look for a top-level `## Mode` section with
`MODE: A` or `MODE: B`. If absent, default to Mode A
(backward-compatible with older callers).

Then dispatch:

- **Mode A:** follow `references/mode-a-incident-absorption.md` for
  Stages 1-9.
- **Mode B:** follow `references/mode-b-live-collaborative.md` for
  its stages (design gates skipped; verification-only).

The rest of this file holds content that is shared across modes:
principles, the reporting-back procedure, validation and scenario
pointers, and the update-vs-create-new rubric.

## Principles

"Repeatable" covers both script-shaped extensions (extra flag, new
output format) and prose-shaped extensions (an additional judgement
step with a stable recipe). Both land inside a skill: scripts under
`scripts/`, judgement steps as SKILL.md prose.

**Reliability is the floor; simplicity is the target.** Default to a
single entry point and one flow. Add surface only when a specific
invariant demands it.

Consult `../crystallize-task-worker/references/spec-summary.md` for
the layout, frontmatter, validation helpers, and scenario template.

## Update-in-place vs. create-new-skill (Mode A only)

**Default to update-in-place.** Only split when the extra work would
plausibly be useful on its own, in a context that does not involve
the existing skill.

- **Update-in-place** when the gap is a natural extension of the
  existing skill (extra flag, new output format, edge case the skill
  did not cover, an additional judgement step in the same flow), OR
  when the gap is only useful in the context of this skill's process
  (you cannot concretely imagine invoking it standalone). The skill's
  identity and primary purpose stay the same.
- **Create-new-skill** when the gap is orthogonal AND has a concrete
  standalone use case -- another agent in another flow would
  reasonably want to invoke it without the existing skill. Pick a
  fresh kebab-case name; the old skill stays untouched. Don't
  decompose proactively for hypothetical reuse.

Script vs prose is orthogonal to this decision. An update-in-place
can land as a new script step, a new prose step, or both. Same for a
create-new-skill.

If update-in-place would double the size of the original SKILL.md or
blur its one-line description, that is a signal to split (combined
with the standalone-use-case check).

In Mode B the decision has already been made by the committed change;
your job is to verify, not to re-litigate.

## Reporting back to the lead

At each gate and at terminal status, communicate with the lead by
writing `runtime/update/reports/report.md` and pushing it back.
Do NOT emit `## GATE:` / `## STATUS:` headers in chat.

Which gates apply:

- **Mode A:** Gate 1 (`outline-approval`), Gate 2 (`final-artifact`).
- **Mode B:** Gate 2 only (`final-artifact`).

Terminal statuses (both modes): `done`, `stuck`, `no-update-needed`.

**Inputs.** Your task file has YAML frontmatter with `lead_agent`,
`lead_report_dir`, and `transcript_path`. Read all three at the start
of your run -- the first two address reports back to the lead, the
third is where Stage 1's incident transcript lives.

**Procedure** at each gate/status:

1. Write `runtime/update/reports/report.md` (create the directory if
   missing):

   ```
   ---
   type: gate | status
   name: <outline-approval | final-artifact | done | stuck | no-update-needed>
   ---

   <body: the message the user needs to see>
   ```

2. Push:

   ```bash
   mngr push <lead_agent>:<lead_report_dir> \
       --source runtime/update/reports/ \
       --uncommitted-changes=merge
   ```

3. Stop your turn.

The push is the ready signal -- only push once the report is fully
written.

## Validation (both modes)

Before emitting Gate 2, validate the target skill's layout:

```bash
uv run .agents/skills/crystallize-task-worker/scripts/validate_skill.py \
    .agents/skills/<name>
```

It must print `ok` and exit 0.

## Scenarios (both modes)

At least one scenario must exercise the new behaviour (the absorbed
incident in Mode A; the changed path in Mode B). Others should
exercise neighbouring or edge paths.

Scenarios are ephemeral -- they live in the transcript for
reproducibility, not on disk. Use the template in
`../crystallize-task-worker/references/spec-summary.md`.

## If you decide no change is needed

Applies to both modes. If the right answer turns out to be "leave
the skill alone", write a terminal report with
`type: status`, `name: no-update-needed`, and body:

```
No update needed. Reason: <one-sentence>.
```

Push it and stop. Do not commit a null change.
