# Absorb flow (lead-side)

Use this flow when a skill ran and you had to do additional *repeatable* work
by hand. The user was not part of a design conversation about the change. The
worker replicates the incident, proposes an outline (Gate 1), implements, runs
scenarios, and presents Gate 2.

This file covers lead-side Steps 2a-2c. Return to `SKILL.md` Step 3 (proxy
gates, merge) afterwards.

## 2a: Capture the incident transcript

Call `extract_turn.py` with the runtime path below; see
`.agents/shared/references/lead-proxy.md` for the full invocation contract
(what `--nth 1` does and how to use marker-based slicing if counting turns
does not line up).

```bash
uv run .agents/shared/scripts/extract_turn.py \
    --nth 1 \
    --output runtime/update/$TARGET/turn.jsonl
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

```bash
mngr create update-$TARGET -t crystallize-worker \
    --label workspace=$MINDS_WORKSPACE_NAME \
    --message-file runtime/update/$TARGET/task.md
```

Then push the runtime dir (task file + transcript) into the worker's worktree
-- the worker cannot read files that live only in your worktree, and its
`parse_task_frontmatter.py` helper needs `task.md` on disk to validate the
schema. See `.agents/shared/references/lead-proxy.md` § "mngr push rationale"
for the directory-form and `--uncommitted-changes=merge` requirements.

```bash
mngr push update-$TARGET:runtime/update/$TARGET/ \
    --source runtime/update/$TARGET/ \
    --uncommitted-changes=merge
```

Return to `SKILL.md` Step 3 to proxy gates through to the user.
