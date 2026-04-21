# agentskills.io layout cheat sheet

The crystallized skills in this project follow the [agentskills.io
spec](https://agentskills.io/specification). This file captures just the
bits you need when building or updating a skill; consult the spec directly
if anything else comes up.

## Directory layout

```
.agents/skills/<name>/
  SKILL.md                  # required; body <= 500 lines (progressive disclosure)
  scripts/
    run.py                  # required for crystallized skills (PEP 723, argparse)
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

## scripts/run.py

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

Two helpers live in the `crystallize-task-worker` scripts directory (they are
installed alongside any worker sub-skill by the crystallize-worker template):

- `scripts/validate_skill_name.py <name>` -- checks the kebab-case rules.
- `scripts/validate_skill.py <skill_dir>` -- checks SKILL.md frontmatter,
  directory name match, description length, 500-line body limit, and PEP 723
  run.py presence for crystallized skills.

Both print `ok` and exit 0 on success; exit 1 with a clear error on failure.

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
