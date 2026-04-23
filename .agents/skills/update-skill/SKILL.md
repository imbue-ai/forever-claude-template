---
name: update-skill
description: Extend or refactor a crystallized skill (or split a new one off) when you had to do additional repeatable work -- script-shaped or prose-shaped -- beyond what the existing skill did. Invoke at turn-end when reflecting on a successful skill use that left you patching around gaps.
---

# Updating or splitting a skill

Use this skill when an existing skill in `.agents/skills/` ran successfully
but you had to do additional *repeatable* work to fully satisfy the user's
request. The goal is to fold that work into the skill (or into a sibling
skill) so it never needs to be redone by hand.

"Repeatable" covers both script-shaped extensions (an extra flag, a new
output format) and prose-shaped extensions (an additional judgement step
with a stable recipe, e.g. "read the output and classify according to
these criteria"). Both fit inside a skill -- scripts in `scripts/`,
judgement as SKILL.md prose.

**Principle.** Reliability is the floor; simplicity is the target. Default to
a single entry point and one flow. Add surface only when a specific invariant
demands it.

Trigger this via the turn-end reflection in CLAUDE.md: "did I do
additional repeatable work the skill could have described itself?" If
yes, invoke update-skill.

## Update vs. create-new: the rubric

**Default to update-in-place.** Only split into a new sibling skill when
the extra work would plausibly be useful on its own -- in a context that
does not involve the existing skill.

- **Update-in-place** when the gap is a natural extension of the existing
  skill (extra flag, new output format, edge case not covered, an
  additional judgement step in the same flow), OR when the gap is only
  useful in the context of this skill's process (you cannot concretely
  imagine invoking it standalone). The skill's identity stays the same.
- **Create-new-skill** when the gap is orthogonal AND has a concrete
  standalone use case -- another agent in another flow would reasonably
  want to invoke it without the existing skill. Don't decompose
  proactively for hypothetical reuse; only split when the standalone case
  is real.

Script vs prose is orthogonal to this decision. Update-in-place and
create-new-skill can each land as scripts, SKILL.md prose, or a mix.

If the extra work was **one-off creative or exploratory** with no
repeatable pattern, it is NOT an update candidate -- it stays with the
main agent. Judgement work with a repeatable recipe IS a candidate; it
becomes a prose step in SKILL.md.

## Conventions

Use `$TARGET` for the skill you are updating (e.g. `migrate-config`). Then:

- Worker agent name: `update-$TARGET`
- Worker branch: `mngr/update-$TARGET`
- Runtime path: `runtime/update/$TARGET/`
- Task file: `/tmp/task-update-$TARGET.md`

## Step 1: Open a tracking ticket

```bash
if command -v tk >/dev/null 2>&1; then
    TICKET_ID=$(tk create "update $TARGET" -t task \
        --acceptance "incident captured; task file written; worker launched; worker DONE; branch merged")
    tk start "$TICKET_ID"
fi
```

## Step 2: Capture the incident transcript

```bash
uv run .agents/skills/crystallize-task/scripts/extract_turn.py \
    --nth 1 \
    --output runtime/update/$TARGET/turn.jsonl
```

The helper auto-discovers the current session transcript via (in order)
`$CLAUDE_TRANSCRIPT_PATH` (set inside hooks), `$MNGR_CLAUDE_SESSION_ID`,
or `$MNGR_AGENT_STATE_DIR/claude_session_id` (the on-disk session id
file, which is always present inside a standard mngr agent).

`--nth 1` selects the *previous* human turn -- the one where the
repeatable-but-manual work was done. `--nth 0` (the default) would
select the current update-skill invocation turn itself.

If counting turns does not line up cleanly (e.g. sub-agent interleaving),
use `--start-marker TEXT` and optionally `--end-marker TEXT` to slice by
matching text content instead.

## Step 3: Write the task file

Describe invariants and state constraints — what the updated skill must
guarantee about its inputs and outputs. Do not enumerate subcommands, flow
steps, or argparse surfaces; surface decisions belong to the worker.

```bash
cat > /tmp/task-update-$TARGET.md << 'TASK_EOF'
# Task: update the `$TARGET` skill (or split a new one)

## Incident
The turn where `$TARGET` was invoked is at
runtime/update/$TARGET/turn.jsonl.

## What the updated skill must do
<state the contract the updated skill must honor after this change — what
inputs it should now accept, what outputs it should now produce. Read the
incident transcript for what was done by hand; here, describe only the new
contract.>

## What to do
Use the `update-skill-worker` sub-skill to: replicate the incident,
decide update-in-place vs. new-sibling-skill, run Gate 1 on the outline,
implement, hand-craft 2-3 scenarios, run them, run Gate 2.

Emit gate questions and status updates inline in your response, using
the headers the sub-skill defines (e.g. `## GATE: outline-approval`,
`## GATE: final-artifact`, `## STATUS: done`). Do NOT call
`send-user-message` or any other channel skill for gates -- the user
reads your response inline.

## Success criteria
- The additional processing no longer needs to be done manually.
- All scenarios pass.
- User has approved outline (Gate 1) and final artifact (Gate 2).
- Work is committed to the worker's branch (`mngr/update-$TARGET`).
TASK_EOF
```

## Step 4: Launch the worker

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

See `.agents/skills/crystallize-task/SKILL.md` Step 4 for the rationale
behind the directory form, the `--uncommitted-changes=merge` flag, and
why `mngr push` (not `mngr file put`) is the correct command.

## Step 5: Proxy gates, then merge

The user sees your chat, not the worker's. The user can view the
worker's chat if they want to, but they are not required to -- so you
drive the worker to completion by proxying its gates and any mid-flow
questions.

Follow the same proxy flow as
`.agents/skills/crystallize-task/SKILL.md` step 5 (subsections 5a-5f).
The capture-based WAITING guardrail in 5c (confirm the worker is
actually at rest via `mngr capture` before reading the transcript)
applies here too -- update workers may also flip to WAITING transiently
between sub-skill invocations.

Substitutions:

- Worker name: `update-$TARGET`
- Branch: `mngr/update-$TARGET`
- Transcript capture path: `/tmp/worker-update-$TARGET-transcript.txt`
- User-approval gates: `## GATE: outline-approval` (Gate 1, where the
  worker also presents the update-in-place vs. create-new-skill
  decision) and `## GATE: final-artifact` (Gate 2).
- Terminal markers: `## STATUS: done` (merge),
  `## STATUS: no-update-needed` (no change — just close the ticket; no
  merge), `## STATUS: stuck` (failure-handling flow).

As a reminder: do not interrupt more recent user work to handle a
worker notification. Answer implementation-detail questions yourself;
escalate Gate 1 and Gate 2 approvals to the user.

If the worker decided "create-new-skill", the new skill lands in its
own directory; the old skill is unchanged.

On successful merge, close the tracking ticket:

```bash
if command -v tk >/dev/null 2>&1 && [ -n "${TICKET_ID:-}" ]; then
    tk close "$TICKET_ID"
fi
```

## Gotchas

- If the target is a built-in skill from the upstream template (e.g.
  `launch-task`, `update-self`), updating it causes local drift from
  upstream. Reconcile later via `update-self` (pull) or
  `submit-upstream-changes` (push).
- Update is non-blocking -- the user's original request is already
  delivered; the update worker just produces a quieter follow-up commit.
