---
name: crystallize-task-worker
description: Turn a crystallization task (a description of work plus verbatim quote anchors that locate it in the lead's transcript) into a committed, reviewed, user-approved skill. Invoke when your task file asks you to crystallize a turn into a new skill.
metadata:
  role: worker-sub-skill
---

# Building a crystallized skill

Your task file describes a turn of work that should become a reusable skill
and gives you verbatim quote anchors for locating it in the lead's
transcript via `mngr transcript`. Follow these stages to go from
"task handed off" to "new skill committed on your branch".

**Principle.** Reliability is the floor; simplicity is the target. Default to
a single entry point and one flow. Add surface only when a specific invariant
demands it.

Consult `.agents/shared/references/spec-summary.md` for the agentskills.io
layout, frontmatter template, PEP 723 script conventions, and the scenario
template you will use in Stage 4.

## Reporting back to the lead

Follow `.agents/shared/references/worker-reporting.md` for the report-file
procedure and task-file frontmatter schema. Substitute:

- `<RUNTIME_REPORTS_DIR>` → `runtime/crystallize/reports/`
- `<TASK_FILE_GLOB>` → `runtime/crystallize/*/task.md`

Valid `name:` values for this worker:

- Gates: `outline-approval` (Stage 2), `final-artifact` (Stage 6).
- Terminal statuses: `done` (Stage 7), `stuck` (see "If you need to give up"
  below).

## Stage 1: Replicate

1. Read the task file. The `## What was done` description and the
   `## Anchors` verbatim quotes are your primary guide.
2. Locate the work being crystallized in the lead's transcript -- follow
   `.agents/shared/references/transcript-exploration.md`.
3. Research the relevant APIs, libraries, and existing utilities you will
   need. Prefer reusing existing functions over reimplementing.
4. If anything is unclear, add your question to the list you will surface
   in Gate 1.

## Stage 2: Propose an outline

Produce a short outline with:

- A kebab-case skill name (see the naming rules in
  `.agents/shared/references/spec-summary.md`).
- A one-paragraph description that states what the skill does AND when to
  use it (this becomes the SKILL.md `description` frontmatter field).
- Inputs: what the skill needs from its caller (CLI args if there's a
  script, or prose parameters if the skill is agent-driven).
- Outputs: what the skill produces (files, stdout, a report the agent
  hands back to the user).
- A step-by-step flow of the skill's process. Tag each step as one of the
  three kinds defined in `.agents/shared/references/spec-summary.md`:
  - `[script]` -- deterministic; lives in `scripts/`.
  - `[ai-script]` -- model judgement that is a fixed part of the flow;
    scripted as a `claude_p.py` call (see spec-summary's "Scripting a
    model step" and the `use-ai-integration` skill for picking the
    pattern). **This is the default for any model-performed step** -- the
    re-run test puts a step here, not in prose, whenever the same
    prompt/criteria run every time with only the data varying.
  - `[prose]` -- executor meta-work only: steps that need the agent in the
    loop and are not part of an automated run.
- Prose justification: for every step you tag `[prose]` that involves a
  model's judgement, state *why* a scripted `claude_p.py` call cannot do
  it (e.g. it needs the live conversation, it gates on user interaction, the
  inputs are chosen by the executor). A model step with no such reason
  belongs in `[ai-script]`. The aim is a flow that runs headless so refresh
  and scheduling work with no extra wiring.
- Subcommand justification: for any subcommand or subflow in the planned
  flow, what invariant makes it separate vs. inlined? If no invariant
  demands separation, inline it.
- A skill with zero `[script]`/`[ai-script]` steps (pure prose recipe) is
  valid only when every step is genuine executor meta-work -- do not invent
  scripts where judgement is the executor's, but do not park model
  judgement in prose to avoid scripting it.
- 2-3 evaluation scenarios you plan to hand-craft (happy path + edge cases).
- Any edge cases you foresaw but chose not to handle (and why).

**You are not bound to the sample's data shape.** The lead may hand you sample
data or scratch scripts (via `source_artifacts_dir`) that fix a particular
output schema, but crystallization is exactly the moment to reconsider how the
task should be done -- including improving the output shape, field names, or
structure. Changing it is allowed and expected; reliability and a clean design
win over matching the scratch shape. When your planned output differs from the
sample the lead handed off, **call that out explicitly in the outline** (and
again at Gate 2): the lead may have surfaces built on the old shape that need
reconciling, and they can only do that if you flag the delta.

### Gate 1: outline approval

Write a report with `type: gate`, `name: outline-approval`, and a body
that contains the outline plus an explicit "Approve this outline? (yes
/ no with notes)" prompt. Push it and stop, per the reporting procedure
above.

Body template:

```
Proposed skill outline:

<paste outline>

Approve this outline? (yes / no with notes)
```

If the user asks for changes, iterate, then emit a fresh
`type: gate, name: outline-approval` report with the revised outline.
Do not proceed to Stage 3 without an explicit yes.

## Stage 3: Build the artifact

Follow the layout and frontmatter conventions in
`.agents/shared/references/spec-summary.md`. Then validate structurally:

```bash
uv run .agents/shared/scripts/validate_skill.py .agents/skills/<name>
```

It must print `ok` before moving on. If it fails, fix and rerun.

## Stage 4: Hand-craft and run scenarios

Pick 2-3 scenarios that exercise the skill end-to-end:

1. **Happy path**: the most common input shape.
2. **Edge case A**: a realistic non-happy input (empty, large, malformed).
3. **Edge case B** (optional): a second non-happy input exercising a
   different code path.

Use the scenario template in `.agents/shared/references/spec-summary.md` to
record each scenario in your transcript. Scenarios are *ephemeral* -- do NOT write
them as files in the skill.

Run each scenario:

- For `[script]` and `[ai-script]` steps: invoke `scripts/run.py` (or the
  relevant helper) with real inputs and inspect the output. An `[ai-script]`
  step makes a real `claude_p.py` call, so it needs credentials at run
  time (`ANTHROPIC_API_KEY` or an inherited `CLAUDE_CONFIG_DIR`); run it on
  a small input and note the `result.cost_usd` it reports.
- For `[prose]` steps: walk through the SKILL.md instructions as if you were
  an agent using the skill, and confirm they produce the expected
  behavior on the scenario's data. Write out this walk-through process; don't just think through it.

If a scenario fails, fix the skill (script or prose). If the skill is
correct but your scenario was wrong, update the scenario.

### Fixture-based tests for skills that parse external data

If the skill's scripts parse external data -- HTML, JSON from
third-party APIs, scraped pages, user-uploaded files -- add a
fixture-based unit test alongside the live-data scenarios above.
Live-data scenarios alone miss a category of bugs that only surface
when a specific input shape hits the parser (e.g. a substring match
that also matches an unintended token, a hardcoded numeric bound, a
date format the parser did not anticipate).

Concretely:
- Save 1-3 representative samples of the external data under
  `.agents/skills/<name>/tests/fixtures/` (small, anonymized if
  applicable).
- Add a `scripts/<name>_test.py` (or similar) that loads each fixture,
  feeds it through the parser, and asserts on the expected shape of
  the output (exact counts, specific field values, edge-case flags).
- Run it as part of Stage 4.

This is strongly recommended -- skipping it is how parser regressions
land. Typical defects that only surface under a concrete input shape:
a substring match that also matches an unintended token (e.g. `jr`
matching inside "major"), a hardcoded numeric bound silently capping
user-specified values, a regex eating whitespace from adjacent fields.
A single fixture-based test catches all of these before they ship.

## Stage 5: Code review and architecture verification

1. Run `/autofix` on your commits. Fix anything the reviewer flags.
2. Run `/imbue-code-guardian:verify-architecture` on your branch. Read
   the verdict. If it flags a blocker, fix it and re-run; if it flags
   non-blockers worth mentioning, surface them in the Gate 2 summary
   below.

Both of these run **before** Stage 6's final-artifact report -- the
user should see a single report that already reflects the review
verdicts, not a report-then-verify-then-report-again pattern.

## Stage 6: Gate 2 -- final artifact approval

Write a report with `type: gate`, `name: final-artifact`, and a body
containing the built-artifact summary plus an approval prompt. Push it
and stop, per the reporting procedure above.

Body template:

```
Built `<name>`:
- SKILL.md: <one-line summary>
- Scripts: <one-line summary per script, or "none -- pure prose skill">
- Scenarios run: <list, with pass/fail>
- Shape changes from the sample: <none, or list the output-schema / field /
  CLI / exit-code deltas a consumer or surface would need to adapt to>

Approve and save? (yes / no with notes)
```

## Stage 7: Commit and hand off

Commit on your current branch, then emit a `name: done` terminal report (body
shape per `.agents/shared/references/worker-reporting.md`). The lead will
merge the branch.

## If you need to give up

If you cannot produce a good artifact, emit a `name: stuck` terminal report
(body shape per `.agents/shared/references/worker-reporting.md`); state in
the body that no skill was saved.

Reasons that genuinely warrant giving up:

- The work turned out to have no stable process across hypothetical re-runs -- each
  re-run would require entirely different steps, not just different
  data.
- You hit a dependency you cannot resolve (e.g. a required service is
  unreachable, a file format you cannot parse).

"Too judgement-heavy to script" is NOT a valid reason to give up. Model
judgement that is a fixed part of the flow is scripted as `claude_p.py`
calls (`[ai-script]`); only genuine executor meta-work stays as SKILL.md
prose. A skill can be pure prose with no scripts at all if every step is
executor meta-work. Only give up if the *process* itself is unstable, not
if parts of it happen to require judgement.
