# agentskills.io layout cheat sheet

The crystallized skills in this project follow the [agentskills.io
spec](https://agentskills.io/specification). This file captures just the
bits you need when building or updating a skill; consult the spec directly
if anything else comes up.

## What a skill is

A skill is a SKILL.md describing a **process**, plus any supporting
scripts, references, or assets. The SKILL.md reads like a recipe: "do X,
then Y, then Z." Each step of that process is one of three kinds:

- **`[script]`** -- deterministic. Runs the same code every time, only the
  data varies. Lives in `scripts/`.
- **`[ai-script]`** -- needs a model's judgement, but is a *fixed part of
  the flow* (the same prompt/criteria every run, only the data varies).
  Script it as a model call via the copyable `claude_p.py` helper (see
  "Scripting a model step" below). This is the **default for any
  model-performed step** -- a step does not drop to prose just because it
  needs judgement.
- **`[prose]`** -- *executor meta-work* that is not part of an automated
  run: choosing inputs, interpreting the final result and deciding what to
  do next, user approval/interaction, anything that needs the live
  conversation context. Written in SKILL.md as instructions the agent using
  the skill follows.

The point of `[ai-script]` is that the whole flow stays runnable headless:
when every flow step (deterministic or model-driven) is scripted, the skill
can be refreshed or scheduled with no additional wiring. Prose is reserved
for the work that genuinely needs the executor in the loop -- not for any
step that happens to require a model. When you leave a model step as
`[prose]`, you must be able to say *why* a scripted `claude -p` call
cannot do it.

A mixed flow of all three kinds is the norm for useful skills.

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

Include `run.py` when the skill has `[script]` or `[ai-script]` steps that
benefit from automation. A skill can be pure SKILL.md prose with no scripts
only when every step is `[prose]` executor meta-work; if any flow step is
deterministic or model-driven, it belongs in a script. Use scripts where
they earn their keep; don't force a script for genuine executor meta-work.

When you do include `run.py`, keep it as simple as the invariants allow
-- default to a single entry point and one flow, and only add subcommands
or subflows when a specific invariant demands the separation.

If the process interleaves deterministic steps with model-judgement steps,
script *both*: the model-judgement steps become `[ai-script]` calls (below)
so the whole chain runs end-to-end without the executor in the loop. Only
break the flow into separate subcommands when a `[prose]` executor step
genuinely sits between two scripted sections.

### Scripting a model step (`[ai-script]`)

A model-judgement step is scripted with the copyable `claude -p` helper,
`claude_p.py`, from the `use-ai-integration` skill: copy that one file into
the skill's own `scripts/` directory and call it from `run.py`. That skill
covers the call surface and the cost model; the short version of the two
entry points:

- `claude_p_completion(prompt, *, system, model=...)` -- no agency
  (classify / summarize / extract / rewrite / answer-from-context). The
  common case for a model-judgement step.
- `claude_p_task(prompt, *, append_system=None, model=...)` -- a one-shot
  agentic step that needs tools or file access.

Both are `async` and return a result carrying `.text`, `.cost_usd`, and
`.usage`. `claude_p.py` is a self-contained PEP 723 snippet, so the copy
sits beside `run.py` and `run.py` stays an ordinary self-contained script --
just list `claude_p.py`'s own deps (`anyio`, `pydantic`) in `run.py`'s PEP
723 header:

```python
# /// script
# requires-python = ">=3.11"
# dependencies = ["anyio", "pydantic>=2"]
# ///
import anyio
from claude_p import claude_p_completion   # the file you copied in

async def main() -> None:
    result = await claude_p_completion(
        prompt,
        system="<a real task instruction, not a placeholder>",
        model="claude-haiku-4-5",
    )
    print(result.text)

anyio.run(main)
```

Invoke it like any skill script -- `uv run
.agents/skills/<name>/scripts/run.py <args>`. For `claude_p_completion`,
`system` is required and must be a real instruction. When a deployment sets
`ANTHROPIC_API_KEY`, a direct `litellm` call is cheaper for non-agentic
work; see `use-ai-integration` for that path and the full cost model.

- Begin every `run.py` with a PEP 723 header pinning its inline deps:
  ```python
  # /// script
  # requires-python = ">=3.11"
  # dependencies = ["rich>=13"]
  # ///
  ```
  A `run.py` that drives a model step copies `claude_p.py` in beside it and
  lists that helper's deps (`anyio`, `pydantic`) here.
- `argparse` entry point; no interactive prompts.
- Stateless by default. If persisted state is genuinely needed, flag it at
  Gate 2 -- don't invent a persistence scheme unilaterally.
- Fail loudly: exit non-zero on error, write the error to stderr.
- Document the invocation in SKILL.md:
  `uv run .agents/skills/<name>/scripts/run.py <args>`

## Validation

`uv run .agents/shared/scripts/validate_skill.py <skill_dir>` checks SKILL.md
frontmatter, the kebab-case name rules, directory-name match, description
length, 500-line body limit, and that any `run.py` begins with a PEP 723
header. Prints `ok` and exits 0 on success; exits 1 with a clear error on
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
