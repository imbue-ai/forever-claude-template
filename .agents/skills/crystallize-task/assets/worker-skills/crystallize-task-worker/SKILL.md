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

**Principle.** Reliability is the floor; simplicity is the target. Default to
a single entry point and one flow. Add surface only when a specific invariant
demands it.

Consult `references/spec-summary.md` for the agentskills.io layout,
frontmatter template, PEP 723 script conventions, and the scenario template
you will use in Stage 4.

## Reporting back to the lead

At each gate (Stage 2 outline, Stage 6 final artifact) and at the
terminal status (Stage 7 done, or "stuck" / "give up" exits), you
communicate with the lead agent by **writing a report file and pushing
it back**. Do NOT emit `## GATE:` / `## STATUS:` headers in chat --
those are not parsed. The lead polls for the pushed file and acts on
it directly.

**Inputs.** Your task file has been synced to your worktree alongside
`turn.jsonl` at `runtime/crystallize/*/task.md`. At the start of your
run, validate its frontmatter and extract the three required fields
with:

```bash
uv run .agents/skills/crystallize-task-worker/scripts/parse_task_frontmatter.py \
    'runtime/crystallize/*/task.md'
```

Quote the glob pattern so the shell passes the literal to the
helper; the helper expands it internally and fails loudly if zero or
more than one task file matches (each worker handles a single task
-- either condition means the runtime layout drifted). On success it
prints three shell-evalable `KEY=value` lines on stdout
(`LEAD_AGENT=`, `LEAD_REPORT_DIR=`, `TRANSCRIPT_PATH=`). It exits
non-zero with a stderr message on any failure, including a missing
or misspelled field or a non-string / empty value. Use `lead_agent`
/ `lead_report_dir` at every gate/status below; `transcript_path`
is where Stage 1's replay transcript lives.

**Procedure.** When you reach a gate or terminal status:

1. Write `runtime/crystallize/reports/report.md` (create the directory
   if missing) with this exact shape:

   ```
   ---
   type: gate | status
   name: <outline-approval | final-artifact | done | stuck>
   ---

   <body: the message the user needs to see, in the voice you would
   have used inside an inline `## GATE:` / `## STATUS:` block -- e.g.
   the proposed outline for Gate 1, the summary + approval prompt for
   Gate 2, the "committed on branch X" note for done, the failure
   explanation for stuck>
   ```

2. Push the report directory to the lead:

   ```bash
   mngr push <lead_agent>:<lead_report_dir> \
       --source runtime/crystallize/reports/ \
       --uncommitted-changes=merge
   ```

   Substitute the actual values from your task file's frontmatter for
   `<lead_agent>` and `<lead_report_dir>`. The trailing slashes matter
   (rsync directory semantics). `--uncommitted-changes=merge` is
   required because the lead's worktree usually has uncommitted local
   state.

3. Stop your turn. For gate reports, the lead will send the user's
   reply via `mngr message` and you will resume; for terminal status
   reports, the lead acts on the report and the run ends.

The push is the ready signal -- it only happens once you are finished
writing. Do not push a partial report.

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
- Inputs: what the skill needs from its caller (CLI args if there's a
  script, or prose parameters if the skill is agent-driven).
- Outputs: what the skill produces (files, stdout, a report the agent
  hands back to the user).
- A step-by-step flow of the skill's process. For each step, tag it as
  either `[script]` (deterministic, will live in `scripts/`) or
  `[prose]` (judgement, will live in SKILL.md as instructions the agent
  using the skill follows). Use the re-run test to decide: if the step
  would run the same code every time with only the data varying, it's a
  script step; if it requires reading output and applying judgement, it's
  a prose step.
- Justification: for any subcommand or subflow in the planned flow, what
  invariant makes it separate vs. inlined? If no invariant demands
  separation, inline it. 
- A skill with zero script steps (pure prose
  recipe) is valid -- do not invent scripts where judgement is clearer.
- 2-3 evaluation scenarios you plan to hand-craft (happy path + edge cases).
- Any edge cases you foresaw but chose not to handle (and why).

### Gate 1: outline approval

Write a report with `type: gate`, `name: outline-approval`, and a body
that contains the outline plus an explicit "Approve this outline? (yes
/ no with notes)" prompt. Push it and stop, per the reporting
procedure at the top of this file.

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
scenario in your transcript. Scenarios are *ephemeral* -- do NOT write
them as files in the skill.

Run each scenario:

- For script steps: invoke `scripts/run.py` (or the relevant helper) with
  real inputs and inspect the output.
- For prose steps: walk through the SKILL.md instructions as if you were
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
land. In the sf-apartment-search crystallization, seven follow-up
autofix commits within the first day were for parser defects (a `jr`
substring matching "major", a price ceiling silently capping
user-specified budgets, a regex eating whitespace from next fields)
that a single fixture-based test would have caught.

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
and stop, per the reporting procedure at the top of this file.

Body template:

```
Built `<name>`:
- SKILL.md: <one-line summary>
- Scripts: <one-line summary per script, or "none -- pure prose skill">
- Scenarios run: <list, with pass/fail>

Approve and save? (yes / no with notes)
```

## Stage 7: Commit and hand off

Commit on your current branch. Then write a terminal report with
`type: status`, `name: done`, and a body like:

```
Committed on branch `<branch-name>`. Ready to merge.
```

Push it (per the reporting procedure at the top of this file) and
stop. The lead will merge the branch.

## If you need to give up

If you cannot produce a good artifact, write a terminal report with
`type: status`, `name: stuck`, and a body like:

```
I could not crystallize this task because: <reason>. No skill was saved.
```

Push it and stop.

Reasons that genuinely warrant giving up:

- The work turned out to have no stable process across hypothetical re-runs -- each
  re-run would require entirely different steps, not just different
  data.
- You hit a dependency you cannot resolve (e.g. a required service is
  unreachable, a file format you cannot parse).

"Too judgement-heavy to script" is NOT a valid reason to give up.
Judgement steps belong in SKILL.md as prose instructions. A skill can be
pure prose with no scripts at all if that's what the process calls for.
Only give up if the *process* itself is unstable, not if parts of it
happen to require judgement.
