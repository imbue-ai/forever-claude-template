---
name: harden-worker
description: Run the background harden pass for one artifact -- crystallize, update, or heal a skill, a service, or the system interface in an isolated worktree, then report back. Invoke when your task file hands you an artifact to harden; it names the operation and artifact to compose.
metadata:
  role: worker-sub-skill
---

# Hardening an artifact (generic worker)

You are the single worker that runs every background harden pass. Your task file
names one **operation** and one **artifact**, and your whole job is to load the
references they select and follow them. You own no operation- or
artifact-specific *behavior* yourself -- that all lives in the references; Step 2
just routes you to the right ones.

## Step 1: Read your task file and resolve inputs

Your task file was synced to your worktree under `runtime/harden/<slug>/task.md`.
Extract the lead address and the report destination (plus the `operation` and
`artifact` fields the lead set in frontmatter):

```bash
eval "$(uv run .agents/shared/scripts/parse_task_frontmatter.py 'runtime/harden/*/task.md')"
```

This sets `LEAD_AGENT`, `FINISH_REPORT_PATH`, `OPERATION`, and `ARTIFACT`. Fail
loudly if `OPERATION` or `ARTIFACT` is unset -- the lead must supply both.

- `OPERATION` is one of `crystallize`, `update`, `heal`.
- `ARTIFACT` is one of `skill`, `service`, `system-interface`.

## Step 2: Load the references that define your run

Read these top to bottom, then follow them.

Filenames are relative to `.agents/shared/worker/references/`, except the two
marked **[shared]**, which live in `.agents/shared/references/`.

| Load when | Reference | What it gives you |
|---|---|---|
| every run | `harden-artifact.md` | the universal contract: the bar, isolation, reporting, testing/hardening, review gates, preserve-and-surface, give-up |
| every run | `op-<OPERATION>.md` | your operation's spine: pre-work, stages, which gates fire and their `name:` values, gate report templates |
| every run | `artifact-<ARTIFACT>.md` | the artifact itself: where it lives, how to run/test it in isolation, how to edit it safely |
| every run | `worker-reporting.md` **[shared]** | the report-file procedure and task-file frontmatter schema (Step 3 uses it) |
| you reconstruct the work from the lead's session | `transcript-exploration.md` | how to find the work in the lead's transcript |
| `ARTIFACT` is `skill` | `spec-summary.md` **[shared]** | the agentskills.io layout/spec authority |
| `ARTIFACT` is `skill`, and your operation runs an outline gate | `skill-outline-fields.md` | what goes inside the outline gate |
| `ARTIFACT` is `skill`, on an emergent `update` | `update-vs-create-new.md` | update-in-place vs. split-a-new-sibling |
| `ARTIFACT` is `service` or `system-interface` | `web-frontend-testing.md` | isolated-instance and rendered-page rules |

The two conditional cases that need defining:

- **"you reconstruct the work from the lead's session"** -- a skill
  `crystallize`, an emergent `update`, or any `heal`: the work or incident you
  must reproduce lives only in the conversation. The other runs are handed a
  materialized source instead and skip `transcript-exploration.md` -- a committed
  `update` reads the diff, a service `crystallize` reads the pre-built service on
  disk, and a system-interface change reads its brief.
- **"runs an outline gate"** -- a skill `crystallize` or an emergent skill
  `update`. A committed `update` and any `heal` have no outline gate, so they
  never use `skill-outline-fields.md` or `update-vs-create-new.md` (loading them
  anyway is harmless).

The operation reference is the lifecycle spine -- it owns the stages, which gates
fire and in what order, and the report templates. The artifact reference is the
operation-agnostic description of the thing you are hardening. Where the operation
reference needs an artifact-specific value (a gate template's field list, the
crystallize shape), it carries that itself, keyed by artifact.

## Step 3: Report back to the lead

Follow `.agents/shared/references/worker-reporting.md` for the report-file
procedure. The `eval` in Step 1 already set the variables it needs. Substitute:

- `<TASK_FILE_GLOB>` -> `runtime/harden/*/task.md`
- `<RUNTIME_REPORTS_DIR>` -> the directory part of `FINISH_REPORT_PATH`
  (i.e. `dirname "$FINISH_REPORT_PATH"`).

The valid `name:` values for gates and terminal statuses come from your
operation reference -- it is the authority on which gates fire for your
operation × artifact combination (e.g. a crystallized service emits no gates; a
crystallized skill emits `outline-approval` then `final-artifact`).

That is the entire worker. Everything else is in the references you loaded in
Step 2.
