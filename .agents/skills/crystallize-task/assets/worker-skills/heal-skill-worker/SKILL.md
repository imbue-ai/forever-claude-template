---
name: heal-skill-worker
description: Repair a failing skill. Invoke when your task file asks you to heal a specific skill by pointing at its incident transcript.
metadata:
  role: worker-sub-skill
---

# Healing a skill

A crystallized or hand-authored skill misbehaved during a real turn; your
job is to fix it.

## Reporting back to the lead

At Gate 2 and at terminal status (done or stuck), communicate with the
lead by writing `runtime/heal/reports/report.md` and pushing it back.

**Inputs.** Your task file has been synced to your worktree alongside
`turn.jsonl` at `runtime/heal/*/task.md`. At the start of your run,
validate its frontmatter and extract the three required fields with:

```bash
uv run .agents/skills/crystallize-task-worker/scripts/parse_task_frontmatter.py \
    'runtime/heal/*/task.md'
```

Quote the glob pattern so the shell passes the literal to the
helper; the helper expands it internally and fails loudly if zero or
more than one task file matches (each worker handles a single task
-- either condition means the runtime layout drifted). On success it
prints three shell-evalable `KEY=value` lines on stdout
(`LEAD_AGENT=`, `LEAD_REPORT_DIR=`, `TRANSCRIPT_PATH=`). It exits
non-zero with a stderr message on any failure, including a missing
or misspelled field or a non-string / empty value. The first two
address reports back to the lead; `transcript_path` is where Stage
1's incident transcript lives.

**Procedure** at each gate/status:

1. Write `runtime/heal/reports/report.md` (create the directory if
   missing):

   ```
   ---
   type: gate | status
   name: <final-artifact | done | stuck>
   ---

   <body: the message the user needs to see>
   ```

2. Push:

   ```bash
   mngr push <lead_agent>:<lead_report_dir> \
       --source runtime/heal/reports/ \
       --uncommitted-changes=merge
   ```

3. Stop your turn.

The push is the ready signal -- only push once the report is fully
written.

## Stage 1: Replicate

1. Read the task file to learn which skill failed and how.
2. Read the incident transcript the task file points at.
3. Read the skill's current `SKILL.md` and any scripts under `<skill_directory>/scripts/`.
   A skill may be pure prose (no scripts), in which case "replicate" means
   tracing the SKILL.md instructions against the incident inputs to see
   where the recipe led astray.
4. Reproduce the failure. If the failure depends on external state you
   cannot recreate, construct a minimal synthetic input that exercises the
   same code path (or, for a prose-only skill, the same branch of the
   recipe).

## Stage 2: Diagnose the root cause

- Trace the actual runtime code path -- do NOT guess from surface
  observations.
- Identify the root cause and briefly write it down in your transcript
  (one or two sentences) before touching code.
- If you cannot identify a root cause confidently, end the turn with
  "could not diagnose" and a description of what you observed. Don't
  apply a speculative fix.

## Stage 3: Apply the fix

- Edit the relevant part of the skill to address the root cause. That
  can be scripts under `scripts/`, `SKILL.md` prose, or both. If the
  root cause was an ambiguous or wrong prose instruction, the fix is a
  SKILL.md edit even if the skill has scripts.
- Keep the fix minimal. Don't refactor unrelated code or prose.
- A heal is a minimal fix. If the fix is growing enough to feel like a
  redesign (new subcommands, new flows, expanded contract, new dependencies), stop and
  escalate to `update-skill` instead.
- Do not add test-only exports or TODO comments.
- Fail loudly on unexpected input rather than silently swallowing.

## Stage 4: Re-run 2-3 fresh scenarios

- Include the incident itself as one scenario.
- Include 1-2 additional scenarios that exercise neighbouring code paths
  to make sure the fix didn't regress anything.
- Scenarios are ephemeral (run in your transcript, not saved to disk).
  Use the template in
  `../crystallize-task-worker/references/spec-summary.md`.

## Stage 5: Code review

Run `/autofix` on your commits. Fix anything the reviewer flags.

## Stage 6: Gate 2 -- approval

Write a report with `type: gate`, `name: final-artifact`, and body:

```
Fixed `<skill-name>`:
- Root cause: <one-sentence>
- Change: <one-sentence>
- Scenarios run: <list, all pass>

Approve the fix? (yes / no with notes)
```

Push it and stop, per the reporting procedure at the top of this file.

## Stage 7: Commit and hand off

Commit on your current branch. Then write a terminal report with
`type: status`, `name: done`, and body:

```
Committed on branch `<branch-name>`. Ready to merge.
```

Push it and stop.

## If you cannot fix it

If the root cause is impossible for you to implement or the right fix would change the skill's
contract in ways the user should decide on, write a terminal report
with `type: status`, `name: stuck`, and body:

```
I could not heal `<skill-name>` because: <reason>. Recommend: <next
step, e.g. a create-new-skill update or manual investigation>.
```

Push it and stop.