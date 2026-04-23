# Mode B: live collaborative update (lead-side)

Use this mode when you and the user discussed a change to a skill
during the turn, agreed on a design, and you committed the change
live. The worker skips Gate 1 (design already approved organically)
and runs verification only: reads the committed change, runs
scenarios, runs `/autofix`, and presents Gate 2 with findings.

This file covers lead-side Steps 2a-2c. Return to `SKILL.md` Step 3
(proxy gates, merge) afterwards.

## 2a: Capture the committed change

Capture the commit metadata and full diff into
`runtime/update/$TARGET/`:

```bash
mkdir -p runtime/update/$TARGET
COMMIT_RANGE="HEAD~1..HEAD"                       # or a broader range
git log --format='%H %s' "$COMMIT_RANGE" \
    > runtime/update/$TARGET/commit.log
git log -p "$COMMIT_RANGE" \
    > runtime/update/$TARGET/commit.diff
```

Adjust `COMMIT_RANGE` to cover all commits that implement the change.
A single-commit change is just `HEAD~1..HEAD`; a multi-commit series
is `<base>..HEAD`. Running this from the main agent's current branch
(where the change was committed) is the simplest invocation.

The worker branch created by `mngr create` is based off the main
agent's current branch, so the committed change is also reachable on
disk in the worker's worktree. The pushed `commit.diff` /
`commit.log` are a convenience index, not a dependency.

## 2b: Write the Mode B task file

The task file carries the design rationale (why this change, what
conversation led here) in your own words. The worker uses the diff
as ground truth and the rationale to judge whether the implementation
matches the agreed design.

```bash
cat > /tmp/task-update-$TARGET.md << TASK_EOF
# Task: verify the live update to \`$TARGET\`

## Mode
MODE: B

## Reporting back
LEAD_AGENT: $MNGR_AGENT_NAME
LEAD_REPORT_DIR: runtime/update/$TARGET/

## Committed change
Branch: $(git branch --show-current)
Commit range: $COMMIT_RANGE
Commits: see runtime/update/$TARGET/commit.log
Diff: see runtime/update/$TARGET/commit.diff

Summary:
<1-3 paragraphs: what changed on disk and why, in your own words.
If multiple commits, list them.>

Design rationale:
<why this design -- the conversation / constraint / invariant that
drove it. This is what the worker checks the diff against.>

## What to do
Use the \`update-skill-worker\` sub-skill in Mode B: skip Gate 1
(design was approved organically in chat), read the committed
change, run 2-3 scenarios (at least one exercising the changed
path), run \`/autofix\`, present Gate 2 with verification findings.

When you reach a gate or terminal status, write a report file to
\`runtime/update/reports/report.md\` and push it to the lead per
the sub-skill's reporting protocol. Do NOT emit \`## GATE:\` /
\`## STATUS:\` headers in chat.

## Success criteria
- The committed change is consistent with the stated design
  rationale (worker reports any divergence at Gate 2).
- All scenarios pass.
- User has approved the final artifact (Gate 2), communicated via
  a pushed report file.
- Any worker-side follow-up fixes are committed on
  \`mngr/update-$TARGET\`.
TASK_EOF
```

The heredoc delimiter is unquoted so the shell substitutes
`$MNGR_AGENT_NAME`, `$TARGET`, `$COMMIT_RANGE`, and the two
`$(...)` git invocations at write time. Shell metacharacters inside
the body (`$`, backticks) are backslash-escaped so they land literal
in the task file.

## 2c: Launch the worker and push the commit artifacts

```bash
mngr create update-$TARGET -t crystallize-worker \
    --label workspace=$MINDS_WORKSPACE_NAME \
    --message-file /tmp/task-update-$TARGET.md
```

Push the `runtime/update/$TARGET/` dir so the worker has
`commit.log` and `commit.diff` under its worktree:

```bash
mngr push update-$TARGET:runtime/update/$TARGET/ \
    --source runtime/update/$TARGET/ \
    --uncommitted-changes=merge
```

Return to `SKILL.md` Step 3 to proxy the Gate 2 (final-artifact)
report through to the user. Mode B emits no Gate 1 report.
