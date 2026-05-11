# Absorb flow (lead-side)

Use this flow when a skill ran and you had to do additional *repeatable* work
by hand. The user was not part of a design conversation about the change. The
worker replicates the incident, proposes an outline (Gate 1), implements, runs
scenarios, and presents Gate 2.

This file covers lead-side Steps 2a-2c. Return to `SKILL.md` Step 3 (proxy
gates, merge) afterwards.

## 2a: Capture the incident transcript

Capture the previous completed turn with `mngr transcript`; see
`.agents/shared/references/lead-proxy.md` for the full invocation contract.

```bash
mkdir -p runtime/update/$TARGET
mngr transcript --last-completed-turn --format jsonl \
    > runtime/update/$TARGET/turn.jsonl
```

## 2b: Write the absorb-flow task file

Describe invariants and state constraints -- what the updated skill must
guarantee about its inputs and outputs. Do not enumerate subcommands, flow
steps, or argparse surfaces; surface decisions belong to the worker.

The task file's YAML frontmatter follows the schema documented in
`.agents/shared/references/worker-reporting.md` -- `lead_agent`,
`lead_report_dir`, and `transcript_path`, all required.

```bash
mkdir -p runtime/update/$TARGET
cat > runtime/update/$TARGET/task.md << TASK_EOF
---
lead_agent: $MNGR_AGENT_NAME
lead_report_dir: runtime/update/$TARGET/reports/
transcript_path: runtime/update/$TARGET/turn.jsonl
---

# Task: update the \`$TARGET\` skill (or split a new one)

## Flow
FLOW: absorb

## Incident
The turn where \`$TARGET\` was invoked is at the path given by the
\`transcript_path\` frontmatter field.

## What the updated skill must do
<state the contract the updated skill must honor after this change --
what inputs it should now accept, what outputs it should now produce.
Read the incident transcript for what was done by hand; here,
describe only the new contract.>

## What to do
Use the \`update-skill-worker\` sub-skill in the absorb flow: replicate
the incident, decide update-in-place vs. new-sibling-skill, run Gate 1
on the outline, implement, hand-craft 2-3 scenarios, run them, run
Gate 2.

When you reach a gate or terminal status, write a report file and
push it to the lead per the sub-skill's reporting protocol; the
destination is given by \`lead_agent\` / \`lead_report_dir\` in
frontmatter.

## Success criteria
- The additional processing no longer needs to be done manually.
- All scenarios pass.
- User has approved outline (Gate 1) and final artifact (Gate 2),
  each communicated via a pushed report file.
- Work is committed to the worker's branch (\`mngr/update-$TARGET\`).
TASK_EOF
```

`FLOW: absorb` is required; the worker fails loudly if the marker is missing.

## 2c: Launch the worker and push the transcript

The shared `launch-task` dispatcher runs `mngr create`, pushes the runtime
dir (task file + transcript) into the worker's worktree, and sends the task
as a follow-up message so the worker sees the runtime dir first. The
worker's `parse_task_frontmatter.py` helper needs `task.md` on disk to
validate the schema, which is why the push is part of the lifecycle.

```bash
uv run .agents/skills/launch-task/scripts/dispatch.py \
    --name update-$TARGET \
    --template crystallize-worker \
    --runtime-dir runtime/update/$TARGET/ \
    --task-file runtime/update/$TARGET/task.md
```

Return to `SKILL.md` Step 3 to proxy gates through to the user.
