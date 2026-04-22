---
name: update-skill-worker
description: Extend an existing skill or split off a new sibling skill. Invoke when your task file asks you to fold additional deterministic processing into an existing skill (or create a sibling for it).
metadata:
  role: worker-sub-skill
---

# Updating (or splitting) a skill

An existing skill ran successfully on a prior turn, but additional
*deterministic* work had to be done by hand to satisfy the user's request.
Your job is to fold that work in -- either by updating the skill in place
or by creating a new sibling skill.

**Principle.** Reliability is the floor; simplicity is the target. Default to
a single entry point and one flow. Add surface only when a specific invariant
demands it.

Both gates apply (outline + final artifact). The outline gate doubles as
the user's chance to veto the update-vs-split decision.

Consult `../crystallize-task-worker/references/spec-summary.md` for the
layout, frontmatter, validation helpers, and scenario template.

## Stage 1: Replicate

1. Read the task file to learn which skill was used and what was missing.
2. Read the incident transcript the task file points at.
3. Read the target skill's current `SKILL.md` and `scripts/run.py`.

## Stage 2: Decide update-in-place vs. create-new

Use this rubric:

- **Update-in-place** when the gap is a natural extension of the existing
  skill: an extra flag, a new output format, an edge case the script did
  not cover. The skill's identity and primary purpose stay the same.
- **Create-new-skill** when the gap is orthogonal -- it happens to chain
  onto the first skill's output, but calling it the same thing would
  confuse future discovery. Pick a fresh kebab-case name; the old skill
  stays untouched.

If update-in-place would double the size of the original SKILL.md or blur
its one-line description, that is a signal to split.

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

End your turn with:

> "Proposed update:
>
> <paste outline, including the update-vs-split decision and reasoning>
>
> Approve this outline? (yes / no with notes)"

Wait for the user's reply before coding.

## Stage 4: Implement

### Update-in-place

- Edit `scripts/run.py` and `SKILL.md` in place. Preserve the existing
  contract for current callers unless the outline explicitly calls for a
  breaking change.
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

End your turn with:

> "<Updated | Created> `<name>`:
> - SKILL.md: <one-line summary of changes>
> - run.py: <one-line summary of changes>
> - Scenarios run: <list, all pass>
>
> Approve and save? (yes / no with notes)"

Wait for the user's reply.

## Stage 9: Commit and hand off

Commit on your current branch. In your final response, state the branch
name so the caller knows what to merge.

## If you decide not to change anything

If the right answer turns out to be "leave the skill alone; the extra
processing was genuinely ad-hoc", end your turn with:

> "No update needed. Reason: <one-sentence>."

and stop. Do not commit a null change.

## Gotchas

- You run with `MNGR_AGENT_ROLE=worker` in the environment. The
  crystallization Stop hook detects this and stays silent, so you will NOT
  see a crystallization reminder after a heavy sub-turn. Don't try to
  recursively crystallize work you do while updating this skill.
