# Mode A: incident absorption (worker flow)

Follow this flow when your task file carries `MODE: A` or omits the
`## Mode` section. Both Gate 1 (outline) and Gate 2 (final artifact)
apply.

## Stage 1: Replicate

1. Read the task file to learn which skill was used and what was
   missing.
2. Read the incident transcript the task file points at (typically
   `runtime/update/<target>/turn.jsonl`).
3. Read the target skill's current `SKILL.md` and any scripts under
   `scripts/`. A skill may be pure prose.

## Stage 2: Decide update-in-place vs. create-new

Apply the rubric in `../SKILL.md` ("Update-in-place vs.
create-new-skill"). Default to update-in-place; only split when the
gap has a concrete standalone use case.

## Stage 3: Propose an outline

Include:

- **Decision**: update-in-place of `<existing-name>`, or
  create-new-skill named `<new-name>`.
- What changes / what the new skill does.
- Inputs, outputs, step-by-step flow.
- Justification: for any subcommand or subflow in the planned flow,
  what invariant makes it separate vs. inlined? If no invariant
  demands separation, inline it.
- 2-3 scenarios you will run.

### Gate 1: outline approval

Write a report with `type: gate`, `name: outline-approval`, and body:

```
Proposed update:

<paste outline, including the update-vs-split decision and
reasoning>

Approve this outline? (yes / no with notes)
```

Push it and stop, per the reporting procedure in `../SKILL.md`. Wait
for the user's reply (delivered via `mngr message`) before coding.

## Stage 4: Implement

### Update-in-place

- Edit the relevant parts of the skill in place: scripts under
  `scripts/`, SKILL.md prose, or both. A new script step goes in
  `scripts/`; a new judgement step goes in SKILL.md as prose
  instructions for the agent using the skill. Preserve the existing
  contract for current callers unless the outline explicitly calls
  for a breaking change.
- Keep SKILL.md under ~500 lines; split long content into
  `references/`.

### Create-new-skill

Delegate to `crystallize-task-worker`: follow its Stage 3 (Build the
artifact) and Stage 4 (Scenarios) using your Gate-1-approved outline
as the input. This avoids restating the layout/validation rules and
keeps a single source of truth. Skip its Gate 1 and Gate 2 -- your
outline has already been approved, and you'll run Gate 2 here.

## Stage 5: Validate

Run `validate_skill.py` per `../SKILL.md`. For both update-in-place
(edited skill) and create-new (new skill), it must print `ok` before
moving on.

## Stage 6: Run 2-3 scenarios

Per `../SKILL.md`. At least one scenario must mimic the original
incident (to prove the manual work is no longer needed). Others
should exercise neighbouring or edge paths.

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

Commit on your current branch. Then write a terminal report with
`type: status`, `name: done`, and body:

```
Committed on branch `<branch-name>`. Ready to merge.
```

Push it and stop.
