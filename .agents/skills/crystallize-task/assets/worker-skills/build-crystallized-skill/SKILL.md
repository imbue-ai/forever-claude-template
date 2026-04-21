---
name: build-crystallized-skill
description: Worker sub-skill that turns a crystallization task (replay transcript + task description) into a committed, reviewed, user-approved skill. Invoke at the start of a crystallize-worker session.
metadata:
  role: worker-sub-skill
---

# Building a crystallized skill (worker flow)

This skill runs inside a `crystallize-worker` sub-agent launched by the main
agent's `crystallize-task` skill. Your task file and the replay transcript
are already on disk; follow these steps to go from "task handed off" to
"new skill committed to a `mngr/<task-name>` branch".

## Stage 1: Replicate

1. Read the task file you were launched with.
2. Read the replay transcript (`runtime/crystallize/<task-name>/turn.jsonl`
   or the path your task file specifies). Understand what tools were called,
   with what inputs, and why.
3. Research the relevant APIs, libraries, and existing utilities you will
   need. Prefer reusing existing functions over reimplementing.
4. If anything is unclear, add your question to the list you will surface in
   Gate 1.

Do NOT re-execute destructive operations from the transcript. Reading the
transcript is enough.

## Stage 2: Propose an outline

Produce a short outline with:

- A kebab-case skill name (1-64 chars, `[a-z0-9-]+`, no leading/trailing or
  consecutive hyphens -- see the validation helper in `scripts/`).
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

The user's reply comes back via `mngr message <your-task-name>`. If they ask
for changes, iterate -- then ask Gate 1 again. Do not proceed to Stage 3
without an explicit yes.

## Stage 3: Build the artifact

### Layout

```
.agents/skills/<name>/
  SKILL.md                  # agentskills.io-compliant; progressive disclosure under ~500 lines
  scripts/
    run.py                  # PEP 723 inline deps; argparse interface
  references/*.md           # optional long-form docs, loaded only when needed
  assets/...                # optional static resources
```

### SKILL.md frontmatter (required)

```yaml
---
name: <skill-name>              # must match parent directory
description: <what-and-when>    # <= 1024 chars
metadata:
  crystallized: true
---
```

### scripts/run.py (required)

- PEP 723 header with pinned inline deps:
  ```python
  # /// script
  # requires-python = ">=3.11"
  # dependencies = ["rich>=13"]
  # ///
  ```
- `argparse` entry point -- no interactive prompts.
- Stateless by default. If the work needs on-disk state, flag it at Gate 2;
  don't invent a persistence scheme.
- Fail loudly: exit non-zero on error, write the error to stderr.
- Document the invocation in SKILL.md: `uv run .agents/skills/<name>/scripts/run.py ...`

### Validation

Before moving to scenarios, run the skill-name validator:

```bash
uv run .agents/skills/build-crystallized-skill/scripts/validate_skill_name.py <name>
```

If it fails, fix the name and try again.

## Stage 4: Hand-craft and run scenarios

Pick 2-3 scenarios that exercise the skill end-to-end:

1. **Happy path**: the most common input shape.
2. **Edge case A**: a realistic non-happy input (empty, large, malformed).
3. **Edge case B** (optional): a second non-happy input exercising a
   different code path.

Run each one by invoking `scripts/run.py` with real inputs and inspecting
the output. Scenarios are *ephemeral* -- they exist in your transcript for
reproducibility, not on disk. Do NOT write them as files in the skill.

If a scenario fails, fix the script. If the script is correct but your
scenario was wrong, update the scenario.

## Stage 5: Code-guardian review

Run `/autofix` on your commits (the `crystallize-worker` template already
enables it). Fix anything the reviewer flags.

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

- Commit to your `mngr/<task-name>` branch.
- Your final response should confirm the branch name so the main agent can
  merge it.

## If you need to give up

If you cannot produce a good artifact (e.g. the work turns out to be too
judgement-heavy to express as a script, or you hit a dependency you cannot
resolve), end your turn with:

> "I could not crystallize this task because: <reason>. No skill was saved."

and stop. The main agent will report this back to the user.
