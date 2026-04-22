# Bias the crystallize/heal/update lifecycle toward simplicity

## Overview

- The crystallize-task, heal-skill, and update-skill lifecycle currently optimizes almost entirely for reliability (gates, scenarios, validation) with nothing that actively pressures against sprawl.
- Symptom observed: a main agent handing off to a worker enumerated 9 subcommands in the task file — the main agent made surface decisions it shouldn't have made, and the worker had no rubric to push back.
- Fix: reframe the lifecycle as "reliability is the floor; simplicity is the target" and enforce the bias at two concrete chokepoints — the task file the main agent writes, and the outline the worker presents at Gate 1.
- Scope is narrow: subcommands and subflows only. Helper functions inside `run.py` are unconstrained — the concern is runtime complexity of the workflow, not internal code organization.
- Existing hand-authored skills (`launch-task`, `send-user-message`, etc.) are out of scope.

## Expected Behavior

- Each of the six lifecycle skills (`crystallize-task`, `heal-skill`, `update-skill`, and the three `*-worker` sub-skills) opens with a short principle paragraph stating: *"Reliability is the floor; simplicity is the target. Default to a single entry point and one flow. Add surface only when a specific invariant demands it."*
- When a main agent writes the task file handed to a worker, the template has a `## Preconditions and postconditions` section (crystallize) or its per-skill equivalent (heal: `## What the fixed skill must do`; update: `## What the updated skill must do`) — the main agent describes what must be true, not what the worker should do.
- The main-skill prose instructions for `crystallize-task` and `update-skill` explicitly prohibit enumerating subcommands, flow steps, or argparse surfaces in the task file. `heal-skill` does not get this prohibition (the target skill already exists; surface is already set).
- At Gate 1, the worker's outline includes an explicit bullet: *"for any helper subcommand or subflow, what invariant makes it separate vs. inlined?"* — so the user sees and can push back on ballooning surface during outline review.
- `heal-skill-worker` gets an additional narrower line: *"a heal is a minimal fix — if the fix is growing enough to feel like a redesign, stop and escalate to `update-skill`."*
- `crystallize-task-worker/references/spec-summary.md` keeps "step-by-step flow" as a first-class outline field (future direction: output artifact), with one added line noting the flow should be as simple as the invariants allow.
- Existing gates (Gate 1 outline approval, Gate 2 final approval), scenarios, and validation helpers stay unchanged. This change is purely additive guidance plus two template edits.

## Changes

**`.agents/skills/crystallize-task/SKILL.md`**
- Add the principle paragraph near the top of the body.
- Add a prose instruction before the "Step 3: Write the task file" section: the task file must describe invariants and state constraints, not enumerate subcommands, flow steps, or argparse surfaces.
- Replace the freeform description in the task-file HEREDOC with a `## Preconditions and postconditions` section and a brief prompt for what goes there.

**`.agents/skills/heal-skill/SKILL.md`**
- Add the principle paragraph near the top of the body.
- Replace the freeform "What went wrong" description block in the task-file HEREDOC with a `## What the fixed skill must do` section (keeping the incident pointer intact) — the main agent states the contract the healed skill must honor.
- No prohibition text — heal targets an existing skill's surface, not a new one.

**`.agents/skills/update-skill/SKILL.md`**
- Add the principle paragraph near the top of the body.
- Add a prose instruction before the "Step 3: Write the task file" section: same prohibition as crystallize-task.
- Replace the freeform "What was missing" description block in the task-file HEREDOC with a `## What the updated skill must do` section.

**`.agents/skills/crystallize-task/assets/worker-skills/crystallize-task-worker/SKILL.md`**
- Add the principle paragraph near the top of the body.
- Add a new bullet to the Stage 2 outline-fields list: *"Justification: for any subcommand or subflow in the planned flow, what invariant makes it separate vs. inlined?"*

**`.agents/skills/crystallize-task/assets/worker-skills/update-skill-worker/SKILL.md`**
- Add the principle paragraph near the top of the body.
- Add the same justification bullet to the Stage 3 outline-fields list.

**`.agents/skills/crystallize-task/assets/worker-skills/heal-skill-worker/SKILL.md`**
- Add the principle paragraph near the top of the body.
- Add one sentence to Stage 3 ("Apply the fix"): a heal is a minimal fix — if it is growing enough to feel like a redesign, stop and escalate to `update-skill`.

**`.agents/skills/crystallize-task/assets/worker-skills/crystallize-task-worker/references/spec-summary.md`**
- Add one line near the outline/scenario section noting the step-by-step flow should be as simple as the invariants allow.
