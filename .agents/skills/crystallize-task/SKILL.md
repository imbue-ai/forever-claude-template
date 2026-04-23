---
name: crystallize-task
description: "Turn a process from the turn that just finished into a reusable skill. A skill captures a stable process -- SKILL.md prose describing the recipe, with scripts for deterministic steps and prose instructions for nondeterministic steps. Consider using after completing a task where a re-run with new inputs would follow a largely similar process. The process does not have to be the entire turn -- a sub-process (e.g. a data pipeline within a larger build) counts. Strong signal: you learned how to do something through research or debugging that is likely to be useful again."
---

# Crystallizing a task into a skill

Use this skill to promote ad-hoc work from the turn that just finished into a
reusable skill consisting of a PEP 723 `scripts/run.py` and a companion
`SKILL.md`, both [agentskills.io](https://agentskills.io/specification)-compliant.
You dispatch the actual build to a sub-agent; your role is to package context,
launch, and merge.

**Principle.** Reliability is the floor; simplicity is the target. Default to
a single entry point and one flow. Add surface only when a specific invariant
demands it. Decompose only when the separate components are likely to be used independently.

## When to invoke

Read `references/when-to-crystallize.md` if you haven't yet for detailed guidelines.

Summary:

1. The work was a single cohesive unit (not a mixed-bag turn that happened to
   touch many files or make many web requests or other tool uses).
2. **Re-run test**: if the user asked you to do this again with different
   inputs, much of the process would be recognizably the same -- same
   sources, same steps, same criteria, just different data. Judgement steps
   in the middle of a flow are fine; they live in SKILL.md as prose
   instructions.
3. You expect this task (or one like it) to recur, either because the user suggested it might or because it seems like a useful task to repeat.

A skill is a SKILL.md (process recipe) plus optional scripts for the
deterministic steps. Judgement steps live in SKILL.md as prose and are
executed by the agent using the skill. Do not demand end-to-end
scriptability before crystallizing.

**Default to asking the user**, not to deciding silently. If you can name
any plausible skill shape, propose it to the user and let them decide.
Only decline outright if the work truly has no stable process across
hypothetical re-runs.

**You don't have to crystallize the entire turn.** Look for reusable
sub-processes within the work. If you learned how to do something --
through research, debugging, or experimentation -- that seems likely to
be useful again, and the process would repeat recognizably, that's a
strong signal to crystallize it.

## Conventions

Pick a short kebab-case slug `$NAME` for this crystallization (e.g.
`migrate-config`). It is used for:

- Worker agent name: `crystallize-$NAME`
- Worker branch: `mngr/crystallize-$NAME` (created by `mngr create`)
- Local artifact paths under `runtime/crystallize/$NAME/`
- Task file path: `/tmp/task-crystallize-$NAME.md`
- `tk` ticket title

Use that same slug everywhere below.

## Step 1: Confirm and open a tracking ticket

**Skip the pre-gate question if the user explicitly invoked this skill.**
Triggers that count as explicit invocation: the user typed
`/crystallize-task`, said "crystallize this / yes crystallize / make a
skill out of this" in the immediately-prior turn, or otherwise named
the skill by hand. In that case go straight to the ticket -- asking
again is redundant and annoying.

Otherwise send a one-line pre-gate question via the `send-user-message` skill:

> "I just did X and Y. Worth crystallizing into a reusable skill? (yes/no)"

Wait for the user's reply. If no, stop here.

If the user said yes (or the skip rule above applied), open a `tk`
ticket so the lifecycle is visible after the turn ends:

```bash
if command -v tk >/dev/null 2>&1; then
    TICKET_ID=$(tk create "crystallize $NAME" -t task \
        --acceptance "transcript extracted; task file written; worker launched; worker DONE; branch merged")
    tk start "$TICKET_ID"
fi
```

If `tk` is not on PATH, skip tracking; the rest of the
skill is unaffected.

## Step 2: Extract the just-finished turn

```bash
uv run .agents/skills/crystallize-task/scripts/extract_turn.py \
    --nth 1 \
    --output runtime/crystallize/$NAME/turn.jsonl
```

The helper auto-discovers the current session transcript via (in order)
`$CLAUDE_TRANSCRIPT_PATH` (set inside hooks), `$MNGR_CLAUDE_SESSION_ID`,
or `$MNGR_AGENT_STATE_DIR/claude_session_id` (the on-disk session id
file, which is always present inside a standard mngr agent). Do not pass
`--transcript` unless you have a specific file to replay.

`--nth 1` selects the *previous* human turn -- the one the user wants
crystallized. `--nth 0` (the default) would select the current
crystallize-task invocation turn itself, which is not what you want.

If counting turns does not line up cleanly (e.g. sub-agent interleaving),
use `--start-marker TEXT` and optionally `--end-marker TEXT` to slice by
matching text content instead.

## Step 3: Write the task file

Describe invariants and state constraints — what must be true about the
skill's inputs and outputs. Do not enumerate subcommands, flow steps, or
argparse surfaces; surface decisions belong to the worker.

```bash
cat > /tmp/task-crystallize-$NAME.md << 'TASK_EOF'
# Task: crystallize the just-finished work into a reusable skill

## Transcript
The turn you need to crystallize is at
runtime/crystallize/$NAME/turn.jsonl (JSONL of tool calls and results).
Replay it mentally to understand what was done; you do not need to
re-execute destructive operations.

## Preconditions and postconditions
<describe what must be true about the skill's inputs before it runs, and
what must be true about its outputs after. Focus on the contract; do not
prescribe subcommands, flow steps, or argparse surfaces — the worker owns
those decisions.>

## What to do
Use the `crystallize-task-worker` sub-skill to drive the end-to-end build.
Emit gate questions and status updates inline in your response, using
the headers the sub-skill defines (e.g. `## GATE: outline-approval`,
`## STATUS: done`). Do NOT call `send-user-message` or any other
channel skill for gates -- the user reads your response inline.

## Worker sub-skills
The `crystallize-task-worker`, `heal-skill-worker`, and
`update-skill-worker` skills have been pre-installed into your
`.agents/skills/` tree.

## Success criteria
- New skill lives at `.agents/skills/<name>/` with SKILL.md (agentskills.io-
  compliant, `metadata.crystallized: true`) and `scripts/run.py` (PEP 723,
  argparse).
- All hand-crafted scenarios pass when run against `scripts/run.py`.
- User has approved both the outline (Gate 1) and the final artifact (Gate 2).
- Work is committed to the worker's branch (`mngr/crystallize-$NAME`).
TASK_EOF
```

## Step 4: Launch the worker

Follow the `launch-task` skill's conventions for worker lifecycle management
(background waiting, checking results, handling outcomes), with these
crystallize-specific overrides:

- Template: `-t crystallize-worker` (not `-t worker`)
- Task file: the one written in step 3

```bash
mngr create crystallize-$NAME -t crystallize-worker \
    --label workspace=$MINDS_WORKSPACE_NAME \
    --message-file /tmp/task-crystallize-$NAME.md
```

The `crystallize-worker` template (see `.mngr/settings.toml`) inherits from
`worker`, sets `MNGR_AGENT_ROLE=worker` so the Stop hook skips inside the
worker, and runs the bundled-sub-skill installer so the worker's
`.agents/skills/` contains `crystallize-task-worker` et al.

Then push the extracted transcript into the worker's worktree -- the
worker cannot read files that live only in the lead's worktree:

```bash
mngr push crystallize-$NAME:runtime/crystallize/$NAME/ \
    --source runtime/crystallize/$NAME/ \
    --uncommitted-changes=merge
```

Notes:
- Use the directory form (trailing slash on both sides). Pushing a
  single file via `--source .../turn.jsonl` fails: rsync interprets the
  source as a directory and errors with `change_dir`.
- `--uncommitted-changes=merge` is required. The worker's worktree has
  uncommitted changes immediately after creation (the installed worker
  sub-skills under `.agents/skills/`), so the default `fail` mode would
  refuse the push.
- There is no `mngr file put` subcommand -- `mngr push` is the
  correct mechanism.

## Step 5: Proxy gates, then merge

The user sees your chat, not the worker's. The user can view the worker's
chat if they want to, but they are not required to -- so you are
responsible for driving the worker to completion by proxying gate
questions and status updates between it and the user.

### 5a. Background the wait

Start `mngr wait` in the background (using the Bash tool with
`run_in_background: true`). You will be notified when the worker
transitions to a terminal state or pauses at a gate -- do not block on
it.

```bash
# Run with Bash run_in_background: true
mngr wait crystallize-$NAME DONE STOPPED WAITING --timeout 30m
```

### 5b. Do not interrupt more recent user work

If the user has given you a more recent task since the worker was
launched, finish that task before acting on the worker's notification.
The notification is informational; act on it once the user's current
request is complete. Do not abandon in-flight work to service a worker
gate.

### 5c. On notification, confirm the worker is actually at rest, then read

`mngr wait ... WAITING` returns on the first RUNNING->WAITING transition.
In practice the worker may flip to WAITING between sub-skill invocations
(e.g. a sub-agent finishes, the top-level loop is momentarily idle, then
the next sub-agent spins up). Treating that transient as a real gate
leads to forwarding an approval while the worker is still working.

Before reading the transcript, confirm the worker has actually stopped:

```bash
mngr capture crystallize-$NAME 2>&1 | tail -40
```

If the pane shows a live spinner (`Committing…`, `Running…`,
`Skill(…) loaded`, a token counter that is still climbing, or an
unfinished tool call), the worker is not at rest. Re-arm a
**terminal-only** wait and come back later:

```bash
# Run with Bash run_in_background: true
mngr wait crystallize-$NAME DONE STOPPED --timeout 30m
```

(Note: `DONE STOPPED` -- no `WAITING` -- so the next notification only
fires on a real terminal transition.)

If the capture looks quiet (prompt ready, no spinner, no in-flight
tool call), proceed: read the worker's latest assistant message.

```bash
mngr transcript crystallize-$NAME --role=assistant \
    > /tmp/worker-crystallize-$NAME-transcript.txt
```

Locate the last line starting with `## GATE: <name>` or
`## STATUS: <name>`. The message body is everything from that header
to the end of the transcript.

If there is no such header, treat it as a failure (see step 5f).

### 5d. On `## GATE: <name>`: decide, forward, re-arm

Read the gate body. Decide whether to answer it yourself or escalate to
the user:

- **Answer yourself** when the question is about implementation details
  the worker could not decide on its own: script structure, argparse
  surface, naming conventions, which utility to reuse, file layout,
  agentskills.io compliance, or anything you can determine from reading
  files or applying the guidelines in
  `.agents/skills/crystallize-task/` and its references.
- **Escalate to the user** when the question turns on user intent (does
  this outline match what you wanted?), scope (should the skill also
  do X?), subjective preference, or domain knowledge you do not have.
  `## GATE: outline-approval` and `## GATE: final-artifact` generally
  escalate.
- **Mix**: if a gate bundles approval (escalate) with pure
  implementation sub-questions (answer yourself), you may pre-answer the
  sub-questions in the message you forward to the user so they do not
  have to weigh in on them.

The worker is framed as addressing the user directly. When you answer,
write your reply in the user's voice and forward it:

```bash
mngr message crystallize-$NAME -m "<reply, in the user's voice>"
```

To escalate, use `send-user-message` to ask the user on your own
channel, wait for their reply, then forward it (verbatim or lightly
massaged) via `mngr message`.

After forwarding, re-arm the wait in the background so the next gate or
terminal status is caught:

```bash
# Run with Bash run_in_background: true
mngr wait crystallize-$NAME DONE STOPPED WAITING --timeout 30m
```

### 5e. On `## STATUS: done`: merge

Merge the worker's branch:

```bash
git fetch . mngr/crystallize-$NAME:mngr/crystallize-$NAME
git merge --no-ff mngr/crystallize-$NAME
```

If the merge conflicts, resolve manually.

On successful merge, close the tracking ticket and optionally destroy
the worker:

```bash
if command -v tk >/dev/null 2>&1 && [ -n "${TICKET_ID:-}" ]; then
    tk close "$TICKET_ID"
fi
# optional: echo "y" | mngr destroy crystallize-$NAME --force
```

### 5f. On `## STATUS: stuck`, no marker, or other terminal failure

Follow `launch-task/references/worker-failure.md`: capture the
transcript for the user, tell the user what happened and where the
evidence lives (branch name, transcript command), and leave the
worker's branch and tmux session intact.

## Guidelines

- Never crystallize without explicit user go-ahead. That go-ahead is
  either a Yes to the Step 1 pre-gate question or, if Step 1's skip
  rule applied, the explicit invocation itself (typing
  `/crystallize-task`, saying "crystallize this", etc.).
- Never crystallize a turn whose process would not repeat recognizably on a
  re-run. If each hypothetical re-run would require entirely different
  steps rather than the same recipe with different data, decline. Note
  that judgement steps within an otherwise stable process do NOT
  disqualify crystallization -- they live in SKILL.md as prose.
- The worker owns outline and implementation decisions. Do not second-guess
  the worker's skill structure unless something is clearly wrong.
- Worker failure handling: see `launch-task/references/worker-failure.md`.
