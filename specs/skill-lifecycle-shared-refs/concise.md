# Share content across the crystallize / heal / update skill family

## Overview

- The three skill-lifecycle flows (`crystallize-task`, `heal-skill`, `update-skill`) plus their worker sub-skills currently duplicate substantial prose and embed shared scripts under `crystallize-task/`. This refactor extracts the shared pieces into a new non-skill directory so each flow owns only its flow-specific content.
- New home: `.agents/shared/` — sibling to `.agents/skills/`, with no SKILL.md so the skill loader ignores it. Contains `scripts/` and `references/` only.
- Worker sub-skills move to live under their parent skill's `assets/worker/` rather than all three being nested inside `crystallize-task/assets/`.
- The `Mode A` / `Mode B` dichotomy in `update-skill` and `update-skill-worker` is renamed to `absorb` / `verify` — both in filenames and in the `MODE: A|B` task-file marker (now `FLOW: absorb|verify`).
- CLAUDE.md's "Proxying worker reports" section is deleted outright (that content is now in the shared references); "Using crystallized skills" shrinks to a 3-5 bullet trigger list.

## Expected Behavior

- Each lead skill (`crystallize-task`, `heal-skill`, `update-skill`) reads as a flow-specific recipe with one-level references into `.agents/shared/references/` for the proxy/reporting mechanics.
- Each worker sub-skill reads as a flow-specific recipe with one-level references into `.agents/shared/references/worker-reporting.md` for the reporting protocol and task-file schema.
- `install_worker_skills.sh`, invoked by the `crystallize-worker` mngr template, walks all three parent-skill `assets/worker/` directories and installs each as `.agents/skills/<parent>-worker/` in the worker worktree. The script itself lives at `.agents/shared/scripts/install_worker_skills.sh` and is referenced by that path in `.mngr/settings.toml`.
- A worker created via `mngr create -t crystallize-worker` finds its sub-skill at the same installed location as before (`.agents/skills/<parent>-worker/`) and has the same externally observable behavior. No change to the report-pushing, gate, or terminal-status contract.
- Task files emitted by `update-skill` carry `FLOW: absorb|verify` instead of `MODE: A|B`. `update-skill-worker` Stage 0 parses `FLOW:` and dispatches accordingly. The parser no longer accepts `MODE:` — this is a hard cutover.
- `parse_task_frontmatter.py` lives at `.agents/shared/scripts/` and is invoked with that path from every worker sub-skill that reads a task file. Its output contract (`LEAD_AGENT=`, `LEAD_REPORT_DIR=`, `TRANSCRIPT_PATH=`) is unchanged.
- `extract_turn.py` / `transcript_parsing.py` live at `.agents/shared/scripts/` and are invoked with that path from every lead skill that extracts a turn. Their CLI surface is unchanged.
- Skill-loader behavior is unchanged: `.agents/shared/` contains no SKILL.md so it does not surface as a skill in any listing.
- A grep sweep over all SKILL.md files under `.agents/skills/` yields zero occurrences of the old paths (`crystallize-task/scripts/extract_turn.py`, `crystallize-task/assets/worker-skills/crystallize-task-worker/scripts/parse_task_frontmatter.py`, `crystallize-task/assets/worker-skills/`).
- Grep for `MODE: A` or `Mode A` or `Mode B` across `.agents/` yields zero occurrences (except possibly inside task-file templates that are now `FLOW:`).
- CLAUDE.md's "Proxying worker reports" section is gone. "Using crystallized skills" exists as 3-5 bullets describing when to invoke each skill, with no proxy/mechanic content.
- The "Reliability is the floor; simplicity is the target" one-liner still appears in its five existing SKILL.md homes — unchanged.
- Giving-up ("if you need to give up" / "if you cannot fix it") content stays inline in each worker SKILL.md with its own flow-specific reason list.

## Changes

### New directory: `.agents/shared/`

- Create `.agents/shared/` as a non-skill directory, sibling to `.agents/skills/`. Contains no SKILL.md.
- `.agents/shared/scripts/` receives: `extract_turn.py`, `extract_turn_test.py`, `transcript_parsing.py`, `transcript_parsing_test.py`, `parse_task_frontmatter.py`, `parse_task_frontmatter_test.py`, `validate_skill.py`, `validate_skill_test.py`, `validate_skill_name.py`, and `install_worker_skills.sh`.
- `.agents/shared/references/` receives three files:
  - `lead-proxy.md` — absorbs the current `crystallize-task/SKILL.md` Step 5 subsections 5a-5e (report polling, frontmatter parse, gate-decision rule, "do not interrupt more recent work", consume/re-arm), plus the shared `mngr push` rationale (directory form, `--uncommitted-changes=merge`, no `mngr file put`), plus the `extract_turn.py` invocation shape and its env-var resolution chain.
  - `worker-reporting.md` — absorbs the current worker "Reporting back to the lead" blocks (write report.md, push it with `mngr push <lead_agent>:<lead_report_dir>`, stop), plus the task-file frontmatter schema (`lead_agent` / `lead_report_dir` / `transcript_path`), plus the `parse_task_frontmatter.py` invocation (including the glob-quoting explanation).
  - `update-vs-create-new.md` — absorbs the update-in-place vs. sibling-split rubric that currently exists in both `update-skill/SKILL.md` and `update-skill-worker/SKILL.md`.

### Worker sub-skills relocated

- Move `crystallize-task/assets/worker-skills/crystallize-task-worker/` → `crystallize-task/assets/worker/`.
- Move `crystallize-task/assets/worker-skills/heal-skill-worker/` → `heal-skill/assets/worker/`.
- Move `crystallize-task/assets/worker-skills/update-skill-worker/` → `update-skill/assets/worker/`.
- Delete `crystallize-task/assets/worker-skills/` after the move.
- Each moved `assets/worker/` directory retains its internal `SKILL.md` and `references/` layout.
- Each moved SKILL.md's `name:` frontmatter field stays as `crystallize-task-worker` / `heal-skill-worker` / `update-skill-worker` so the installed skill identity is unchanged.

### Worker sub-skill internal references

- `crystallize-task/assets/worker/references/spec-summary.md` stays in place (skill-specific content).
- Replace `update-skill/assets/worker/references/mode-a-incident-absorption.md` → `update-skill/assets/worker/references/worker-absorb.md`.
- Replace `update-skill/assets/worker/references/mode-b-live-collaborative.md` → `update-skill/assets/worker/references/worker-verify.md`.
- The worker sub-skill SKILL.md that dispatches on FLOW (`update-skill-worker`) reads `worker-absorb.md` or `worker-verify.md` based on the `FLOW:` frontmatter field.

### Lead-side reference file renames (update-skill only)

- Replace `update-skill/references/mode-a-incident-absorption.md` → `update-skill/references/lead-absorb.md`.
- Replace `update-skill/references/mode-b-live-collaborative.md` → `update-skill/references/lead-verify.md`.

### `install_worker_skills.sh` rewrite

- Move the script to `.agents/shared/scripts/install_worker_skills.sh`.
- Rewrite to iterate over a hardcoded list (or auto-discovery) of parent-skill worker sources: `.agents/skills/crystallize-task/assets/worker/`, `.agents/skills/heal-skill/assets/worker/`, `.agents/skills/update-skill/assets/worker/`.
- For each source, copy into `<destination>/<parent>-worker/` preserving the existing installed-name convention.
- Fail loudly (non-zero) if a source is missing or if two sources would collide on the destination name.
- Update `.mngr/settings.toml`'s `crystallize-worker.extra_provision_command` to invoke the new path: `bash .agents/shared/scripts/install_worker_skills.sh .agents/skills`.

### Task-file marker cutover: `MODE:` → `FLOW:`

- In `update-skill/references/lead-absorb.md` and `lead-verify.md`: the task-file heredoc writes `FLOW: absorb` or `FLOW: verify` (respectively) instead of `MODE: A` / `MODE: B`.
- In `update-skill/assets/worker/SKILL.md` Stage 0: replace the `MODE: A|B` detection and dispatch logic with `FLOW: absorb|verify`. No default — absence of `FLOW:` is a hard error (each task-file writer must emit it).
- In `update-skill/SKILL.md` body prose: replace "Mode A" with "absorb flow" and "Mode B" with "verify flow" throughout.
- In `update-skill/assets/worker/SKILL.md` body prose: same replacement.
- `update-vs-create-new.md` body prose: same replacement.

### Cross-reference updates

- Every SKILL.md under `.agents/skills/` that currently references `crystallize-task/scripts/extract_turn.py`, `crystallize-task/scripts/transcript_parsing.py`, or `crystallize-task/assets/worker-skills/crystallize-task-worker/scripts/*.py` is updated to cite `.agents/shared/scripts/<filename>`.
- `crystallize-task/SKILL.md`, `heal-skill/SKILL.md`, `update-skill/SKILL.md`: Step 5 (proxy gates, merge) subsections are replaced with a single reference: "Follow `.agents/shared/references/lead-proxy.md` for polling, gate decisions, and `mngr push` rationale." Flow-specific substitutions (poll path, branch name, which gate names apply, which terminal statuses) stay inline.
- `crystallize-task/assets/worker/SKILL.md`, `heal-skill/assets/worker/SKILL.md`, `update-skill/assets/worker/SKILL.md`: the "Reporting back to the lead" block is replaced with a single reference: "Follow `.agents/shared/references/worker-reporting.md` for the report-file procedure and task-file schema." Flow-specific `name:` enums (e.g. `done | stuck | no-update-needed`) stay inline.
- `update-skill/SKILL.md` and `update-skill/assets/worker/SKILL.md`: the update-vs-create-new rubric is replaced with a single reference to `.agents/shared/references/update-vs-create-new.md`.
- The `extract_turn.py` env-var resolution chain and `--start-marker` escape hatch are removed from the three lead SKILL.md files — callers just invoke the script at its new path and rely on `--help` / the script's docstring.
- The `parse_task_frontmatter.py` glob-quoting explanation is removed from the three worker SKILL.md files — callers just invoke the script with the quoted glob per the example in `worker-reporting.md`.

### CLAUDE.md changes

- Delete the entire "## Proxying worker reports" section (lines 145-181) including the bulleted body and "Full flow details: see step 5 of `.agents/skills/crystallize-task/SKILL.md`" tail. That content now lives in `.agents/shared/references/lead-proxy.md`, cited from each lead SKILL.md.
- Rewrite the "## Using crystallized skills" section (lines 203-224) as a short 3-5 bullet trigger list: (a) after a Stop-hook crystallization nudge, judge whether to invoke `crystallize-task`; (b) after a skill errors or delivers a wrong result, invoke `heal-skill` at turn-end; (c) after a successful skill use that still required manual post-processing, invoke `update-skill` at turn-end; (d) heal/update/crystallize are all non-blocking follow-ups — the user's immediate request has already been delivered. Drop the explanation of how they dispatch workers, since that is flow-internal.
- Leave every other CLAUDE.md section untouched (including `send-user-message`, `tk` ticket, Memory, Services, Git, etc.). Reorganization of those is out of scope.

### Preserve-as-is

- The "Reliability is the floor; simplicity is the target" principle stays in its five existing inline copies. No shared file.
- "Give up" / "if you cannot fix it" content stays inline in each worker SKILL.md with its skill-specific reason list.
- `crystallize-task/assets/worker/references/spec-summary.md` (agentskills.io layout cheat sheet) stays owned by crystallize-task-worker — it is only invoked during skill creation, which is crystallize-specific.

### Migration

- Single clean-cutover commit covers the file moves, `install_worker_skills.sh` rewrite, `.mngr/settings.toml` path update, `MODE:`→`FLOW:` swap, and all SKILL.md / CLAUDE.md text edits.
- No symlinks, no backward-compatibility shims, no transition period.

### Verification

- Run `bash .agents/shared/scripts/install_worker_skills.sh /tmp/iws-test` and diff the produced tree against expected layout: `/tmp/iws-test/crystallize-task-worker/`, `/tmp/iws-test/heal-skill-worker/`, `/tmp/iws-test/update-skill-worker/`, each containing its SKILL.md and `references/` subdirectory.
- Grep every path referenced in `.agents/skills/**/SKILL.md`, `.agents/skills/**/references/*.md`, and the three shared reference files. Every cited path must resolve on disk post-move.
- Run each moved script's `_test.py` suite at its new location: `extract_turn_test.py`, `transcript_parsing_test.py`, `parse_task_frontmatter_test.py`, `validate_skill_test.py`. All must pass — catches import-path breakage from the relocation.
- Grep `.agents/` for `"MODE: A"`, `"Mode A"`, `"Mode B"`, `mode-a-`, `mode-b-`. Expect zero hits.
- Grep `.agents/` for the old shared-script paths (`crystallize-task/scripts/extract_turn.py`, `crystallize-task/assets/worker-skills/`). Expect zero hits.
- Full end-to-end `mngr create crystallize-<test>` run inside the template container is flagged as a manual user follow-up — the current environment is not the template's container, so the agent cannot exercise it.
