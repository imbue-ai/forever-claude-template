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
  Script it as an AI call following the `use-ai-integration` skill (see
  "Scripting a model step" below). This is the **default for any
  model-performed step** -- a step does not drop to prose just because it
  needs judgement.
- **`[prose]`** -- *user-in-the-loop work*: a step that needs the user
  present while the skill runs (see the test below). Written in SKILL.md as
  instructions the agent using the skill follows.

The point of `[ai-script]` is that the whole flow stays runnable headless:
once every flow step is scripted, the skill can be refreshed or scheduled
with no extra wiring. `[prose]` is reserved for work that genuinely needs the
executor in the loop -- never for a step that merely needs a model.

### The test: `[ai-script]` vs `[prose]`

Two things that feel like reasons for prose are not. **Needing a model's
judgement** isn't one -- that's exactly what `[ai-script]` is for. **Needing
the current conversation** isn't one either -- a script can fetch the
transcript and pass it into the prompt (this skill's own crystallize/update
workers run headless that way). So a script can assemble its own inputs, and
the decisive question is not about any single step but about the skill's
**execution mode**:

> Does this skill need the user *in the loop while it runs* -- for their live
> input (an approval, a decision, an answer to a clarifying question), or
> because they invoke it interactively to follow along and steer?

- **If no** -- the typical fetch / transform / judge skill, and the vast
  majority of cases -- every step is scriptable (`[script]` / `[ai-script]`)
  and the skill has no `[prose]`.
- **If yes** -- the steps that genuinely need the user are `[prose]`. You
  must be able to name which kind of involvement each needs; if you can't, it
  belongs in `[ai-script]`.

(Merely wanting to *watch* an automatable skill run is not a reason for
prose: with proper subcommand decomposition in the script, you can just choose
at runtime to go step by step if that's what the user seems to want)

### Push prose to the edges

Interactive involvement usually lands at the *edges* -- an approval or input
choice up front, a decision about the result at the back -- so the healthy
shape is **prose at the edges, scripted steps in the middle**. A `[prose]`
step wedged *between* two scripted sections is the expensive case: it splits
the pipeline into two halves that can't compose, which is what stops the flow
from running unattended. Only accept it when the user must genuinely intervene
mid-run (a mandatory sign-off before a destructive step, a human steering
what runs next); otherwise there is almost certainly an `[ai-script]` you
haven't written yet.

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

When you do include `run.py`, write the flow's logic as small helper
functions (one per step) and expose them two ways:

- **A subcommand per step**, whenever the step's inputs and outputs serialize
  cleanly -- data, not live handles. (A step that hands the next one an open
  browser session, a DB connection, or a large in-memory object stays inlined;
  splitting it is not reasonable.) Once the steps are already helper functions
  the split is cheap, and it pays off twice: an agent running the skill in a
  chat turn can drive the steps one at a time for a rich per-step progress
  view, and each boundary leaves an inspectable intermediate artifact.
- **A `run all` subcommand** that chains the steps in-process -- just function
  calls, no serialization cost -- for headless and scheduled runs.

Both entry styles call the same helper functions, so the logic has one source
of truth. The per-step split *is* the structure; don't invent subflows beyond
the skill's natural steps.

If the process interleaves deterministic and model-judgement steps, script
*both* (the model steps become `[ai-script]` calls -- see below) so the whole
chain runs end-to-end.

### Packaging

Packaging: `run.py` should be an ordinary self-contained PEP 723 script. 
For [ai-script] steps, make sure to read and follow the instructions in the **`use-ai-integration`** skill;
add appropriate dependencies to your PEP 723 header.

- Begin every `run.py` with a PEP 723 header pinning its inline deps:
  ```python
  # /// script
  # requires-python = ">=3.11"
  # dependencies = ["rich>=13"]
  # ///
  ```
- `argparse` entry point; no interactive prompts.
- Stateless across runs by default -- transient per-step I/O between
  subcommands is fine, durable cross-run state is not. If durable state is
  genuinely needed, flag it at Gate 2 -- don't invent a persistence scheme
  unilaterally.
- Fail loudly: exit non-zero on error, write the error to stderr.
- Document the invocation in SKILL.md:
  `uv run .agents/skills/<name>/scripts/run.py <args>`

## Validation

`uv run .agents/shared/scripts/validate_skill.py <skill_dir>` checks SKILL.md
frontmatter, the kebab-case name rules, directory-name match, description
length, 500-line body limit, and that any `run.py` begins with a PEP 723
header. When those static checks pass and a `run.py` exists, it also runs
`uv run scripts/run.py --help`, which forces `uv` to resolve the script's PEP
723 dependencies and import the module -- so a broken import or unresolvable
dependency fails validation here rather than only at scenario time. (This is a
shallow import check: `--help` exercises top-level imports and the argparse
wiring, not imports done lazily inside subcommand bodies -- those are left to
scenario testing.) Prints `ok` and exits 0 on success; exits 1 with a clear
error on failure.

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
