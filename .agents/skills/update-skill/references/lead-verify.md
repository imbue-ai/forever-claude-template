# Verify flow (lead-side)

Use this flow when you and the user discussed a change to a skill during the
turn, agreed on a design, and you committed the change live. The worker skips
Gate 1 (design already approved organically) and runs verification only:
reads the committed change, runs scenarios, runs `/autofix`, and presents
Gate 2 with findings.

This file covers lead-side Steps 2a-2c. Return to `SKILL.md` Step 3 (proxy
gates, merge) afterwards.

## 2a: Capture the committed change

Capture the commit metadata and full diff into `runtime/update/$TARGET/`:

```bash
mkdir -p runtime/update/$TARGET
COMMIT_RANGE="HEAD~1..HEAD"                       # or a broader range
git log --format='%H %s' "$COMMIT_RANGE" \
    > runtime/update/$TARGET/commit.log
git log -p "$COMMIT_RANGE" \
    > runtime/update/$TARGET/commit.diff
```

Adjust `COMMIT_RANGE` to cover all commits that implement the change. A
single-commit change is just `HEAD~1..HEAD`; a multi-commit series is
`<base>..HEAD`. Running this from the main agent's current branch (where the
change was committed) is the simplest invocation.

The worker branch created by `mngr create` is based off the main agent's
current branch, so the committed change is also reachable on disk in the
worker's worktree. The pushed `commit.diff` / `commit.log` are a convenience
index, not a dependency.

## 2b: Write the verify-flow task file

The task file carries the design rationale (why this change, what conversation
led here) in your own words. The worker uses the diff as ground truth, the
rationale to judge whether the implementation matches the agreed design, and
your lead transcript (via `mngr transcript`) to recover any conversational
context the rationale alludes to.

The task file's YAML frontmatter follows the schema documented in
`.agents/shared/references/worker-reporting.md`.

```bash
cat > runtime/update/$TARGET/task.md << TASK_EOF
---
lead_agent: $MNGR_AGENT_NAME
finish_report_path: runtime/update/$TARGET/reports/report.md
---

# Task: verify the live update to \`$TARGET\`

## Flow
FLOW: verify

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

## Anchors (verbatim quotes)
The worker can use these with \`mngr transcript <lead_agent>\` to
recover conversational context referenced by the rationale (e.g. why a
particular alternative was rejected). Include 1-3 short quotes from
the user that pinned down the design.
<paste quotes here, one per bullet.>

## What to do
Use the \`update-skill-worker\` sub-skill in the verify flow: skip
Gate 1 (design was approved organically in chat), read the committed
change, run 2-3 scenarios (at least one exercising the changed
path), run \`/autofix\`, present Gate 2 with verification findings.

When you reach a gate or terminal status, write a report file and
push it to the lead per the sub-skill's reporting protocol; the
destination is given by \`lead_agent\` / \`finish_report_path\` in
frontmatter.

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

## 2c: Launch the worker

The `runtime/update/$TARGET/` push carries `task.md`, `commit.log`, and
`commit.diff` into the worker's worktree.

```bash
uv run .agents/skills/launch-task/scripts/create_worker.py launch \
    --name update-$TARGET \
    --template crystallize-worker \
    --runtime-dir runtime/update/$TARGET/ \
    --task-file runtime/update/$TARGET/task.md
```

Return to `SKILL.md` Step 3 to proxy the Gate 2 (final-artifact) report
through to the user. The verify flow emits no Gate 1 report.
