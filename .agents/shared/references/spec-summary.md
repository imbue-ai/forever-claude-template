# agentskills.io layout cheat sheet

The crystallized skills in this project follow the [agentskills.io
spec](https://agentskills.io/specification). This file captures just the
bits you need when building or updating a skill; consult the spec directly
if anything else comes up.

## What a skill is

A skill is a SKILL.md describing a **process**, plus any supporting
scripts, references, or assets. The SKILL.md reads like a recipe: "do X,
then Y, then Z." Any given step can be "run this script" (deterministic)
or "read the output and apply these criteria" (judgement -- executed by
the agent using the skill).

Judgement steps are part of the skill, written as prose in SKILL.md. Do
not try to engineer them out. A mixed flow of scripts and prose
instructions is the norm for useful skills, not the exception.

## Directory layout

```
.agents/skills/<name>/
  SKILL.md                  # required; body <= 500 lines (progressive disclosure)
  scripts/
    run.py                  # optional; include when there are deterministic steps
    *.py                    # optional helpers
  references/*.md           # optional long-form docs; load on demand
  assets/...                # optional static resources (templates, samples)
```

The `name` used in `.agents/skills/<name>/` must match the `name` field in
SKILL.md frontmatter (1-64 chars, lowercase letters/digits + single hyphens,
no leading/trailing or consecutive hyphens).

## SKILL.md frontmatter

Minimum required:

```yaml
---
name: <skill-name>              # must match parent directory
description: <what-and-when>    # 1-1024 chars; describe behavior + triggers
metadata:
  crystallized: true            # set for skills produced by this lifecycle
---
```

Omit `allowed-tools`, `license`, and `compatibility` unless you have a
specific reason to constrain or declare them -- the defaults are fine.

## scripts/run.py (optional)

Include `run.py` when the skill has deterministic steps that benefit from
automation. A skill can also be pure SKILL.md prose with no scripts at
all, if every step is judgement or uses existing tools directly. Use
scripts where they earn their keep; don't force a script when prose is
clearer.

When you do include `run.py`, keep it as simple as the invariants allow
-- default to a single entry point and one flow, and only add subcommands
or subflows when a specific invariant demands the separation.

If the process looks like: <deterministic steps> -> <nondeterministic judgments made by you> -> <deterministic steps>
you can encode that as subcommands on `run.py` to do the deterministic sections as separate steps.

- PEP 723 header with pinned inline deps:
  ```python
  # /// script
  # requires-python = ">=3.11"
  # dependencies = ["rich>=13"]
  # ///
  ```
- `argparse` entry point; no interactive prompts.
- Stateless by default. If persisted state is genuinely needed, flag it at
  Gate 2 -- don't invent a persistence scheme unilaterally.
- Fail loudly: exit non-zero on error, write the error to stderr.
- Document the invocation in SKILL.md:
  `uv run .agents/skills/<name>/scripts/run.py <args>`

## Validation

`uv run .agents/shared/scripts/validate_skill.py <skill_dir>` checks SKILL.md
frontmatter, the kebab-case name rules, directory-name match, description
length, 500-line body limit, and PEP 723 run.py presence for crystallized
skills. Prints `ok` and exits 0 on success; exits 1 with a clear error on
failure.

## Scenario template

Scenarios are *ephemeral* -- they exist in your transcript for
reproducibility, not on disk. Do NOT save scenarios as files in the skill.
Record each scenario in the transcript in this form:

```
### Scenario: <one-line description>
- Command: `uv run .agents/skills/<name>/scripts/run.py <args>`
- Input: <stdin / files / env / CLI args>
- Expected: <exit code + stdout/file contents assertion>
- Actual: <observed>
- Status: pass | fail
```
