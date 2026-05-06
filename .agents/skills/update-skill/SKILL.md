---
name: update-skill
description: Extend, refactor, or verify a contract-bearing artifact that other skills or the agent depend on -- a crystallized skill under `.agents/skills/`, a shared script or reference under `.agents/shared/` (e.g. `extract_turn.py`, `lead-proxy.md`), or a template/config file with a documented contract (`.mngr/settings.toml` create_templates, `services.toml`, hook scripts, `CLAUDE.md` policy text). Invoke at turn-end when you had to do additional repeatable work around the artifact (absorb flow -- e.g. you worked around a bug in a shared script with a manual flag), or when you and the user discussed a change and you applied it live (verify flow -- e.g. you just edited and committed a template config). Not for arbitrary application code -- the signal is *contract-bearing* / *consumed by other skills or the agent*, not raw file location.
---

# Updating or splitting a skill

Use this skill when a **contract-bearing artifact** other skills or the
agent depend on needs a change. The skill is named for its most common
target (crystallized skills under `.agents/skills/`), but the same
absorb/verify pipeline applies to any of these classes:

- Crystallized skills under `.agents/skills/`.
- Shared scripts and references under `.agents/shared/` consumed by other
  skills (e.g. `scripts/extract_turn.py`, `references/lead-proxy.md`,
  `references/worker-reporting.md`).
- Template / config files with a documented contract -- `.mngr/settings.toml`
  create_templates, `services.toml` service definitions, hook scripts with
  a documented contract, `CLAUDE.md` policy text. Anything where a change
  in one place ripples to behavior other skills or the agent rely on.

This is *not* "edit any file". Ordinary application or product code edits
go through the regular dev loop. The trigger here is that the artifact
carries a contract -- if you change it inline without ratification, other
skills or the agent at large can drift silently. The worker pipeline adds
the rigor (scenario testing, validation, gated approval) that's awkward
to do interactively.

Two flows cover the two ways this happens.

- **absorb flow.** The artifact was used (or relied on) but you had to do
  additional *repeatable* work to fully satisfy the user's request --
  e.g. you patched around a bug in a shared script with a manual flag, or
  did extra processing the skill should have handled. The user was not
  part of a design conversation about the change. You hand the worker the
  incident transcript and the new contract; the worker replicates, proposes
  a design at Gate 1, implements, runs scenarios, presents Gate 2.
- **verify flow.** You and the user discussed a change to the artifact
  during the turn, agreed on a design, and you committed the change live
  -- e.g. you just edited and committed a `.mngr/settings.toml`
  create_template, a `services.toml` entry, or prose in a SKILL.md. You
  hand the worker the committed diff and the design rationale. The worker
  skips Gate 1 (the design was approved organically in chat), reads the
  committed change, runs scenarios, runs `/autofix`, and presents Gate 2
  with verification findings.

Pick the verify flow when the user was explicitly in the design loop for
this change *and* the change is already committed on the branch.
Otherwise pick the absorb flow. Absorb is the safe default; its Gate 1
just re-surfaces the design for an approval pass.

"Repeatable" covers both script-shaped extensions (an extra flag, a new
output format) and prose-shaped extensions (an additional judgement step
with a stable recipe). Both fit inside a skill -- scripts in `scripts/`,
judgement as SKILL.md prose.

**Principle.** Reliability is the floor; simplicity is the target.
Default to a single entry point and one flow. Add surface only when a
specific invariant demands it.

Whether to update-in-place or split a new sibling skill is a decision the
worker makes in the absorb flow; the rubric lives at
`.agents/shared/references/update-vs-create-new.md`. In the verify flow
the decision is already made by the committed change.

## Conventions

Use `$TARGET` for the skill you are updating (e.g. `migrate-config`).
Then:

- Worker agent name: `update-$TARGET`
- Worker branch: `mngr/update-$TARGET`
- Runtime path: `runtime/update/$TARGET/`
- Task file: `runtime/update/$TARGET/task.md` (sits alongside `turn.jsonl`
  / `commit.diff` so the absorb / verify `mngr push` syncs it to the
  worker for free)

## Step 1: Open a tracking ticket

Shared across flows.

```bash
TICKET_ID=$(tk create "update $TARGET" -t task \
    --acceptance "incident captured; task file written; worker launched; worker DONE; branch merged")
tk start "$TICKET_ID"
```

## Step 2: Prepare artifacts, task file, and launch the worker

The two flows differ in what they capture (transcript vs. committed diff)
and in the task-file body (new-contract prose vs. design rationale). The
task file carries a `FLOW: absorb|verify` marker so the worker routes
correctly; the marker is required.

- **absorb:** follow `references/lead-absorb.md`.
- **verify:** follow `references/lead-verify.md`.

Each reference walks you through capturing the artifact, writing the task
file, running `mngr create`, and running any needed `mngr push`. Return
here afterwards.

## Step 3: Proxy gates, then merge

Follow `.agents/shared/references/lead-proxy.md` for polling, gate
decisions, the "do not interrupt more recent user work" rule, and `mngr
push` rationale.

Flow-specific substitutions:

- Worker name: `update-$TARGET`
- Branch: `mngr/update-$TARGET`
- Poll path: `runtime/update/$TARGET/reports/report.md`
- Consumed path: `runtime/update/$TARGET/reports/consumed/`
- User-approval gates:
  - **absorb:** `type: gate, name: outline-approval` (Gate 1, where the
    worker also presents the update-in-place vs. create-new-skill
    decision) and `type: gate, name: final-artifact` (Gate 2).
  - **verify:** `type: gate, name: final-artifact` (Gate 2 only; the
    design was approved organically in chat, so no Gate 1).
- Terminal statuses: `type: status, name: done` (merge);
  `type: status, name: no-update-needed` (no change -- just close the
  ticket; no merge); `type: status, name: stuck` (failure-handling flow).

On successful merge, close the tracking ticket:

```bash
tk close "$TICKET_ID"
```

## Gotchas

- If the target is a built-in skill from the upstream template (e.g.
  `launch-task`, `update-self`), updating it causes local drift from
  upstream. Reconcile later via `update-self` (pull) or
  `submit-upstream-changes` (push).
- Update is non-blocking -- the user's original request is already
  delivered; the update worker produces a quieter follow-up commit.
- The verify flow's worker may produce no new commits of its own if
  verification is clean. That is expected -- the substantive change is
  already on the current branch.
