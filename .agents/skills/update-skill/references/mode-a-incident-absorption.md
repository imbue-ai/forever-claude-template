# Mode A: incident absorption (lead-side)

Use this mode when a skill ran and you had to do additional
*repeatable* work by hand. The user was not part of a design
conversation about the change. The worker replicates the incident,
proposes an outline (Gate 1), implements, runs scenarios, and
presents Gate 2.

This file covers lead-side Steps 2a-2c. Return to `SKILL.md` Step 3
(proxy gates, merge) afterwards.

## 2a: Capture the incident transcript

```bash
uv run .agents/skills/crystallize-task/scripts/extract_turn.py \
    --nth 1 \
    --output runtime/update/$TARGET/turn.jsonl
```

The helper auto-discovers the current session transcript via (in
order) `$CLAUDE_TRANSCRIPT_PATH` (set inside hooks),
`$MNGR_CLAUDE_SESSION_ID`, or
`$MNGR_AGENT_STATE_DIR/claude_session_id` (the on-disk session id
file, which is always present inside a standard mngr agent).

`--nth 1` selects the *previous* human turn -- the one where the
repeatable-but-manual work was done. `--nth 0` (the default) would
select the current update-skill invocation turn itself.

If counting turns does not line up cleanly (e.g. sub-agent
interleaving), use `--start-marker TEXT` and optionally
`--end-marker TEXT` to slice by matching text content instead.

## 2b: Write the Mode A task file

Describe invariants and state constraints -- what the updated skill
must guarantee about its inputs and outputs. Do not enumerate
subcommands, flow steps, or argparse surfaces; surface decisions
belong to the worker.

```bash
cat > /tmp/task-update-$TARGET.md << TASK_EOF
# Task: update the \`$TARGET\` skill (or split a new one)

## Mode
MODE: A

## Reporting back
LEAD_AGENT: $MNGR_AGENT_NAME
LEAD_REPORT_DIR: runtime/update/$TARGET/

## Incident
The turn where \`$TARGET\` was invoked is at
runtime/update/$TARGET/turn.jsonl.

## What the updated skill must do
<state the contract the updated skill must honor after this change --
what inputs it should now accept, what outputs it should now produce.
Read the incident transcript for what was done by hand; here,
describe only the new contract.>

## What to do
Use the \`update-skill-worker\` sub-skill in Mode A: replicate the
incident, decide update-in-place vs. new-sibling-skill, run Gate 1
on the outline, implement, hand-craft 2-3 scenarios, run them,
run Gate 2.

When you reach a gate or terminal status, write a report file to
\`runtime/update/reports/report.md\` and push it to the lead per
the sub-skill's reporting protocol. Do NOT emit \`## GATE:\` /
\`## STATUS:\` headers in chat -- the lead reads the report file,
not your transcript.

## Success criteria
- The additional processing no longer needs to be done manually.
- All scenarios pass.
- User has approved outline (Gate 1) and final artifact (Gate 2),
  each communicated via a pushed report file.
- Work is committed to the worker's branch (\`mngr/update-$TARGET\`).
TASK_EOF
```

The heredoc delimiter is unquoted so `$MNGR_AGENT_NAME` and `$TARGET`
expand; shell metacharacters inside the body (`$`, backticks) are
backslash-escaped so they land literal in the task file.

`MODE: A` is explicit but optional -- the worker defaults to A when
the marker is absent.

## 2c: Launch the worker and push the transcript

```bash
mngr create update-$TARGET -t crystallize-worker \
    --label workspace=$MINDS_WORKSPACE_NAME \
    --message-file /tmp/task-update-$TARGET.md
```

Then push the extracted transcript into the worker's worktree -- the
worker cannot read files that live only in the lead's worktree:

```bash
mngr push update-$TARGET:runtime/update/$TARGET/ \
    --source runtime/update/$TARGET/ \
    --uncommitted-changes=merge
```

See `.agents/skills/crystallize-task/SKILL.md` Step 4 for the
rationale behind the directory form, `--uncommitted-changes=merge`,
and why `mngr push` (not `mngr file put`) is correct.

Return to `SKILL.md` Step 3 to proxy gates through to the user.
