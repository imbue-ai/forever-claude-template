# Absorb flow (worker)

Follow this flow when your task file carries `FLOW: absorb`. Both Gate 1
(outline) and Gate 2 (final artifact) apply.

## Stage 1: Replicate

1. Read the task file. The `## Incident summary` description and the
   `## Anchors` verbatim quotes are your primary guide.
2. Locate the incident and the manual follow-up in the lead's transcript
   -- follow `.agents/shared/references/transcript-exploration.md`.
3. Read the target skill's current `SKILL.md` and any scripts under
   `scripts/`. A skill may be pure prose.

## Stage 2: Decide update-in-place vs. create-new

Apply the rubric in `.agents/shared/references/update-vs-create-new.md`.
Default to update-in-place; only split when the gap has a concrete standalone
use case.

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

Push it and stop, per the reporting procedure in `../SKILL.md`. Wait for the
user's reply (delivered via `mngr message`) before coding.

## Stage 4: Implement

### Update-in-place

- Edit the relevant parts of the skill in place: scripts under `scripts/`,
  SKILL.md prose, or both. A new script step goes in `scripts/`; a new
  judgement step goes in SKILL.md as prose instructions for the agent using
  the skill. Preserve the existing contract for current callers unless the
  outline explicitly calls for a breaking change.
- Keep SKILL.md under ~500 lines; split long content into `references/`.

### Cross-section alignment sweep

After the localized edit, sweep the rest of the SKILL.md (and any
sibling `references/*.md`) and update every cross-reference point that
names or summarizes the changed material.

Sweep checklist (skip anything not present in the file):

- **Frontmatter `description`** -- often a one-liner that summarizes
  the body or names the skill's matching surface. If the change
  touches scope, framing, or matching surface, the description usually
  needs to follow.
- **H1 / opening prose** -- the title and the paragraph after it tend
  to summarize the whole body. Update if the change shifts the skill's
  purpose or the headline framing.
- **`## Principles` bullets (or equivalent top-of-file summaries)** --
  these are often one-line abstractions of a Step further down. If you
  rewrote the underlying Step, the bullet must follow.
- **Section headings** -- "Step 4: Validate the core capability first"
  describes its body; if the body's framing changes, the heading must
  too.
- **Subsection examples and analogies** -- callouts inside a section
  that illustrate the section's framing.
- **Cross-references between sections** -- "see Step 5", "as
  established in Step 4", etc. become wrong if section numbers or
  framings shift.
- **`## Conventions` and `## Gotchas` (if present)** -- frequently
  reference earlier sections by topic and need re-checking when those
  sections move.

Treat the sweep as part of the substantive edit, not a follow-up.

### Create-new-skill

Delegate to `crystallize-task-worker`: follow its Stage 3 (Build the
artifact) and Stage 4 (Scenarios) using your Gate-1-approved outline as the
input. This avoids restating the layout/validation rules and keeps a single
source of truth. Skip its Gate 1 and Gate 2 -- your outline has already been
approved, and you'll run Gate 2 here.

## Stage 5: Validate

Run `validate_skill.py` per `../SKILL.md`. For both update-in-place (edited
skill) and create-new (new skill), it must print `ok` before moving on.

## Stage 6: Run 2-3 scenarios

Per `../SKILL.md`. At least one scenario must mimic the original incident (to
prove the manual work is no longer needed). Others should exercise
neighbouring or edge paths.

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

Push it and stop, per the reporting procedure in `../SKILL.md`.

## Stage 9: Commit and hand off

Commit on your current branch, then emit a `name: done` terminal report (body
shape per `.agents/shared/references/worker-reporting.md`).
