---
name: update-skill-worker
description: Extend an existing skill or split off a new sibling skill. Invoke when your task file asks you to fold additional processing into an existing skill (or create a sibling for it).
metadata:
  role: worker-sub-skill
---

# Updating (or splitting) a skill

An existing skill ran successfully on a prior turn, but additional
*repeatable* work had to be done by hand to satisfy the user's request.
Your job is to fold that work in -- either by updating the skill in place
or by creating a new sibling skill.

"Repeatable" covers both script-shaped extensions (extra flag, new output
format) and prose-shaped extensions (an additional judgement step with a
stable recipe). Both land inside a skill: scripts under `scripts/`,
judgement steps as SKILL.md prose.

**Principle.** Reliability is the floor; simplicity is the target. Default to
a single entry point and one flow. Add surface only when a specific invariant
demands it.

Both gates apply (outline + final artifact). The outline gate doubles as
the user's chance to veto the update-vs-split decision.

Consult `../crystallize-task-worker/references/spec-summary.md` for the
layout, frontmatter, validation helpers, and scenario template.

## Reporting back to the lead

At each gate (Stage 3 outline, Stage 8 final artifact) and at terminal
status (done, stuck, or no-update-needed), communicate with the lead
by writing `runtime/update/reports/report.md` and pushing it back.
Do NOT emit `## GATE:` / `## STATUS:` headers in chat.

**Inputs.** Your task file contains a `## Reporting back` section with
`LEAD_AGENT: <name>` and `LEAD_REPORT_DIR: <path>`. Read both at the
start of your run.

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
   mngr push <LEAD_AGENT>:<LEAD_REPORT_DIR> \
       --source runtime/update/reports/ \
       --uncommitted-changes=merge
   ```

3. Stop your turn.

The push is the ready signal -- only push once the report is fully
written.

## Stage 1: Replicate

1. Read the task file to learn which skill was used and what was missing.
2. Read the incident transcript the task file points at.
3. Read the target skill's current `SKILL.md` and any scripts under
   `scripts/`. A skill may be pure prose.

## Stage 2: Decide update-in-place vs. create-new

**Default to update-in-place.** Only split when the extra work would
plausibly be useful on its own, in a context that does not involve the
existing skill.

- **Update-in-place** when the gap is a natural extension of the existing
  skill (extra flag, new output format, edge case the skill did not
  cover, an additional judgement step in the same flow), OR when the gap
  is only useful in the context of this skill's process (you cannot
  concretely imagine invoking it standalone). The skill's identity and
  primary purpose stay the same.
- **Create-new-skill** when the gap is orthogonal AND has a concrete
  standalone use case -- another agent in another flow would reasonably
  want to invoke it without the existing skill. Pick a fresh kebab-case
  name; the old skill stays untouched. Don't decompose proactively for
  hypothetical reuse.

Script vs prose is orthogonal to the update-vs-split decision. An
update-in-place can land as a new script step, a new prose step, or
both. Same for a create-new-skill.

If update-in-place would double the size of the original SKILL.md or blur
its one-line description, that is a signal to split (combined with the
standalone-use-case check).

## Stage 3: Propose an outline

Include:

- **Decision**: update-in-place of `<existing-name>`, or create-new-skill
  named `<new-name>`.
- What changes / what the new skill does.
- Inputs, outputs, step-by-step flow.
- Justification: for any subcommand or subflow in the planned flow, what
  invariant makes it separate vs. inlined? If no invariant demands
  separation, inline it.
- 2-3 scenarios you will run.

### Gate 1: outline approval

Write a report with `type: gate`, `name: outline-approval`, and body:

```
Proposed update:

<paste outline, including the update-vs-split decision and reasoning>

Approve this outline? (yes / no with notes)
```

Push it and stop, per the reporting procedure at the top of this
file. Wait for the user's reply (delivered via `mngr message`) before
coding.

## Stage 4: Implement

### Update-in-place

- Edit the relevant parts of the skill in place: scripts under
  `scripts/`, SKILL.md prose, or both. A new script step goes in
  `scripts/`; a new judgement step goes in SKILL.md as prose
  instructions for the agent using the skill. Preserve the existing
  contract for current callers unless the outline explicitly calls for
  a breaking change.
- Keep SKILL.md under ~500 lines; split long content into `references/`.

### Create-new-skill

Delegate to `crystallize-task-worker`: follow its Stage 3 (Build the
artifact) and Stage 4 (Scenarios) using your Gate-1-approved outline as
the input. This avoids restating the layout/validation rules and keeps a
single source of truth. Skip its Gate 1 and Gate 2 -- your outline has
already been approved, and you'll run Gate 2 here.

## Stage 5: Validate

```bash
uv run .agents/skills/crystallize-task-worker/scripts/validate_skill.py .agents/skills/<name>
```

Run this for both update-in-place (against the edited skill) and create-new
(against the new skill). It must print `ok` before moving on.

## Stage 6: Run 2-3 scenarios

- At least one scenario must mimic the original incident (to prove the
  manual work is no longer needed).
- Others should exercise neighbouring or edge paths.
- Scenarios are ephemeral. Use the template in
  `../crystallize-task-worker/references/spec-summary.md`.

## Stage 7: Code review

Run `/autofix` on your commits. Fix anything the reviewer flags.

## Stage 8: Gate 2 -- final artifact

Write a report with `type: gate`, `name: final-artifact`, and body:

```
<Updated | Created> `<name>`:
- SKILL.md: <one-line summary of changes, or "unchanged">
- Scripts: <one-line summary per changed/added script, or "unchanged" / "none">
- Scenarios run: <list, all pass>

Approve and save? (yes / no with notes)
```

Push it and stop, per the reporting procedure at the top of this file.

## Stage 9: Commit and hand off

Commit on your current branch. Then write a terminal report with
`type: status`, `name: done`, and body:

```
Committed on branch `<branch-name>`. Ready to merge.
```

Push it and stop.

## If you decide not to change anything

If the right answer turns out to be "leave the skill alone; the extra
processing was genuinely ad-hoc", write a terminal report with
`type: status`, `name: no-update-needed`, and body:

```
No update needed. Reason: <one-sentence>.
```

Push it and stop. Do not commit a null change.
