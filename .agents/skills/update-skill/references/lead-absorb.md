# Absorb flow (lead-side)

Use this flow when a skill ran and you had to do additional *repeatable* work
by hand. The user was not part of a design conversation about the change. The
worker replicates the incident, proposes an outline (Gate 1), implements, runs
scenarios, and presents Gate 2.

This file covers lead-side Step 2 (write the task file). Return to `SKILL.md`
Step 3 (proxy gates, merge) afterwards.

## 2a: Write the absorb-flow task file

The worker will explore your transcript via `mngr transcript` to find the
incident. Your job here is to write a task body that *describes* the
incident and the additional repeatable work, then anchors the worker's
search with verbatim quotes (the user's request, the failing or
insufficient output of `$TARGET`, the manual follow-up steps you took).
Without anchors the worker will scan the wrong region of your transcript.

Describe invariants and state constraints -- what the updated skill must
guarantee about its inputs and outputs. Do not enumerate subcommands, flow
steps, or argparse surfaces; surface decisions belong to the worker.

The task file's YAML frontmatter follows the schema documented in
`.agents/shared/references/worker-reporting.md` -- `lead_agent` and
`lead_report_dir`, both required.

```bash
mkdir -p runtime/update/$TARGET
cat > runtime/update/$TARGET/task.md << TASK_EOF
---
lead_agent: $MNGR_AGENT_NAME
lead_report_dir: runtime/update/$TARGET/reports/
---

# Task: update the \`$TARGET\` skill (or split a new one)

## Flow
FLOW: absorb

## Incident summary
<2-5 sentences: what the user asked for, how \`$TARGET\` was invoked,
where it fell short, and the additional repeatable work you did to
fully satisfy the request.>

## Anchors (verbatim quotes)
The worker will use these to locate the incident in your transcript via
\`mngr transcript\`. Include:
- The user's original request (verbatim).
- The \`$TARGET\` output that was insufficient (verbatim).
- 1-2 quotes showing the manual follow-up you did or the final result.
<paste quotes here, one per bullet.>

## What the updated skill must do
<state the contract the updated skill must honor after this change --
what inputs it should now accept, what outputs it should now produce.
Describe only the new contract; the incident itself is captured above.>

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

Fill in the `## Incident summary` and `## Anchors` sections with real
content drawn from your conversation -- do not leave the placeholders.
`FLOW: absorb` is required; the worker fails loudly if the marker is missing.

## 2b: Launch the worker

```bash
uv run .agents/skills/launch-task/scripts/create_worker.py launch \
    --name update-$TARGET \
    --template crystallize-worker \
    --runtime-dir runtime/update/$TARGET/ \
    --task-file runtime/update/$TARGET/task.md
```

Return to `SKILL.md` Step 3 to proxy gates through to the user.
