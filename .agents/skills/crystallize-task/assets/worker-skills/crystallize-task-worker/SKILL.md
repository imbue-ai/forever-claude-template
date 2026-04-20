---
name: crystallize-task-worker
description: Turn a crystallization task (a replay transcript plus a task description) into a committed, reviewed, user-approved skill. Invoke when your task file asks you to crystallize a turn into a new skill.
metadata:
  role: worker-sub-skill
---

# Building a crystallized skill

Your task file describes a turn of work that should become a reusable skill
and points at a replay transcript on disk. Follow these stages to go from
"task handed off" to "new skill committed on your branch".

Consult `references/spec-summary.md` for the agentskills.io layout,
frontmatter template, PEP 723 script conventions, and the scenario template
you will use in Stage 4.

## Stage 1: Replicate

1. Read the task file.
2. Read the replay transcript it points at. Understand what tools were
   called, with what inputs, and why.
3. Research the relevant APIs, libraries, and existing utilities you will
   need. Prefer reusing existing functions over reimplementing.
4. If anything is unclear, add your question to the list you will surface
   in Gate 1.

Do NOT re-execute destructive operations from the transcript. Reading the
transcript is enough.

## Stage 2: Propose an outline

Produce a short outline with:

- A kebab-case skill name (see the naming rules in
  `references/spec-summary.md`).
- A one-paragraph description that states what the skill does AND when to
  use it (this becomes the SKILL.md `description` frontmatter field).
- Inputs: CLI arguments the script will take.
- Outputs: what the script prints / writes / returns.
- A step-by-step flow of what the script does.
- 2-3 scenarios you plan to hand-craft (happy path + edge cases).
- Any edge cases you foresaw but chose not to handle (and why).

### Gate 1: outline approval

End your turn with:

> "Proposed skill outline:
>
> <paste outline>
>
> Approve this outline? (yes / no with notes)"

If the user asks for changes, iterate -- then ask Gate 1 again. Do not
proceed to Stage 3 without an explicit yes.

## Stage 3: Build the artifact

Follow the layout and frontmatter conventions in
`references/spec-summary.md`. Then validate structurally:

```bash
uv run .agents/skills/crystallize-task-worker/scripts/validate_skill_name.py <name>
uv run .agents/skills/crystallize-task-worker/scripts/validate_skill.py .agents/skills/<name>
```

Both must print `ok` before moving on. If either fails, fix and rerun.

## Stage 4: Hand-craft and run scenarios

Pick 2-3 scenarios that exercise the skill end-to-end:

1. **Happy path**: the most common input shape.
2. **Edge case A**: a realistic non-happy input (empty, large, malformed).
3. **Edge case B** (optional): a second non-happy input exercising a
   different code path.

Use the scenario template in `references/spec-summary.md` to record each
scenario in your transcript. Run each one by invoking `scripts/run.py` with
real inputs and inspecting the output. Scenarios are *ephemeral* -- do NOT
write them as files in the skill.

If a scenario fails, fix the script. If the script is correct but your
scenario was wrong, update the scenario.

## Stage 5: Code review

Run `/autofix` on your commits. Fix anything the reviewer flags.

## Stage 6: Gate 2 -- final artifact approval

End your turn with:

> "Built `<name>`:
> - SKILL.md: <one-line summary>
> - run.py: <one-line summary>
> - Scenarios run: <list, with pass/fail>
>
> Approve and save? (yes / no with notes)"

Wait for the user's reply.

## Stage 7: Commit and hand off

Commit on your current branch. In your final response, state the branch
name so the caller knows what to merge.

## If you need to give up

If you cannot produce a good artifact (e.g. the work turns out to be too
judgement-heavy to express as a script, or you hit a dependency you cannot
resolve), end your turn with:

> "I could not crystallize this task because: <reason>. No skill was saved."

and stop.

## Gotchas

- You run with `MNGR_AGENT_ROLE=worker` in the environment. The
  crystallization Stop hook detects this and stays silent, so you will NOT
  see a crystallization reminder after a heavy sub-turn. Don't try to
  recursively crystallize work you do while building this skill.
