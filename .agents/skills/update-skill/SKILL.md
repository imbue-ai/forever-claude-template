---
name: update-skill
description: Extend or refactor a crystallized skill (or split a new one off). Invoke at turn-end when you had to do additional repeatable work around an existing skill (Mode A, incident absorption), or when you and the user explicitly discussed a change to a skill and you applied it live (Mode B, live collaborative update).
---

# Updating or splitting a skill

Use this skill when an existing skill in `.agents/skills/` needs a
change. Two modes cover the two ways this happens.

- **Mode A (incident absorption) -- default.** A skill ran
  successfully but you had to do additional *repeatable* work to
  fully satisfy the user's request. The user was not part of a design
  conversation about the change. You hand the worker the incident
  transcript and the new contract; the worker replicates, proposes a
  design at Gate 1, implements, runs scenarios, presents Gate 2.
- **Mode B (live collaborative update).** You and the user discussed
  a change to a skill during the turn, agreed on a design, and you
  committed the change live. You hand the worker the committed diff
  and the design rationale. The worker skips Gate 1 (the design was
  approved organically in chat), reads the committed change, runs
  scenarios, runs `/autofix`, and presents Gate 2 with verification
  findings.

Pick Mode B when the user was explicitly in the design loop for this
skill change *and* the change is already committed on the branch.
Otherwise pick Mode A. Mode A is the safe default; its Gate 1 just
re-surfaces the design for an approval pass.

"Repeatable" covers both script-shaped extensions (an extra flag, a
new output format) and prose-shaped extensions (an additional
judgement step with a stable recipe). Both fit inside a skill --
scripts in `scripts/`, judgement as SKILL.md prose.

**Principle.** Reliability is the floor; simplicity is the target.
Default to a single entry point and one flow. Add surface only when a
specific invariant demands it.

## Update vs. create-new: the rubric

This decision belongs to the worker (Mode A); it is included here so
you can anticipate the worker's choice. In Mode B the decision has
already been made -- it is whatever the live commit implemented.

**Default to update-in-place.** Only split into a new sibling skill
when the extra work would plausibly be useful on its own -- in a
context that does not involve the existing skill.

- **Update-in-place** when the gap is a natural extension of the
  existing skill (extra flag, new output format, edge case not
  covered, an additional judgement step in the same flow), OR when
  the gap is only useful in the context of this skill's process.
- **Create-new-skill** when the gap is orthogonal AND has a concrete
  standalone use case. Don't decompose proactively for hypothetical
  reuse.

Script vs prose is orthogonal to this decision.

If the extra work was **one-off creative or exploratory** with no
repeatable pattern, it is NOT an update candidate -- it stays with
the main agent. Judgement work with a repeatable recipe IS a
candidate; it becomes a prose step in SKILL.md.

## Conventions

Use `$TARGET` for the skill you are updating (e.g. `migrate-config`).
Then:

- Worker agent name: `update-$TARGET`
- Worker branch: `mngr/update-$TARGET`
- Runtime path: `runtime/update/$TARGET/`
- Task file: `runtime/update/$TARGET/task.md` (sits alongside `turn.jsonl`
  / `commit.diff` so the Mode A / Mode B `mngr push` syncs it to the
  worker for free)

## Step 1: Open a tracking ticket

Shared across modes.

```bash
if command -v tk >/dev/null 2>&1; then
    TICKET_ID=$(tk create "update $TARGET" -t task \
        --acceptance "incident captured; task file written; worker launched; worker DONE; branch merged")
    tk start "$TICKET_ID"
fi
```

## Step 2: Prepare artifacts, task file, and launch the worker

The two modes differ in what they capture (transcript vs. committed
diff) and in the task-file body (new-contract prose vs. design
rationale). The task file carries a `MODE: A|B` marker so the worker
routes correctly; if absent the worker defaults to A.

- **Mode A:** follow `references/mode-a-incident-absorption.md`.
- **Mode B:** follow `references/mode-b-live-collaborative.md`.

Each reference walks you through capturing the artifact, writing the
task file, running `mngr create`, and running any needed `mngr push`.
Return here afterwards.

## Step 3: Proxy gates, then merge

Follow the same file-based proxy flow as
`.agents/skills/crystallize-task/SKILL.md` step 5 (subsections 5a-5e).
Poll for `runtime/update/$TARGET/reports/report.md`; when it appears,
parse the frontmatter and act.

Substitutions:

- Worker name: `update-$TARGET`
- Branch: `mngr/update-$TARGET`
- Poll path: `runtime/update/$TARGET/reports/report.md`
- Consumed path: `runtime/update/$TARGET/reports/consumed/`
- User-approval gates:
  - **Mode A:** `type: gate, name: outline-approval` (Gate 1, where
    the worker also presents the update-in-place vs. create-new-skill
    decision) and `type: gate, name: final-artifact` (Gate 2).
  - **Mode B:** `type: gate, name: final-artifact` (Gate 2 only; the
    design was approved organically in chat, so no Gate 1).
- Terminal statuses: `type: status, name: done` (merge);
  `type: status, name: no-update-needed` (no change -- just close
  the ticket; no merge); `type: status, name: stuck`
  (failure-handling flow).

As a reminder: do not interrupt more recent user work to handle a
report notification. Answer implementation-detail questions yourself;
escalate Gate 1 and Gate 2 approvals to the user.

If the worker decided "create-new-skill" (Mode A), the new skill
lands in its own directory; the old skill is unchanged.

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
  delivered; the update worker produces a quieter follow-up commit.
- Mode B's worker may produce no new commits of its own if
  verification is clean. That is expected -- the substantive change
  is already on the current branch.
