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

Everything you need is loaded here, at the top level -- the references below
never send you off to load further references mid-run. Read them in this order.

**Always, in order:**

1. `.agents/shared/worker/references/harden-artifact.md` -- the universal contract (the
   bar, isolation, reporting, testing/hardening, review gates,
   preserve-and-surface, give-up).
2. `.agents/shared/worker/references/op-<OPERATION>.md` -- your operation's pre-work,
   stages, the exact gate / terminal-status `name:` values that apply, and the
   gate report body templates (keyed by artifact where they differ).
3. `.agents/shared/worker/references/artifact-<ARTIFACT>.md` -- what the artifact is:
   where it lives, how to run/test it in isolation, scenario specifics, and how
   to work on it safely.
4. `.agents/shared/references/worker-reporting.md` -- the report-file procedure
   and task-file frontmatter schema (Step 3 uses it).
5. `.agents/shared/worker/references/transcript-exploration.md` -- how to find the
   work in the lead's transcript. (Skip only when you are crystallizing a
   pre-built service or verifying an already-committed update -- those read the
   artifact or the diff directly rather than the transcript.)

**Then, conditionally:**

- `ARTIFACT` is `skill` -> also read
  `.agents/shared/references/spec-summary.md` (the agentskills.io layout/spec
  authority). When the operation designs the skill (`crystallize`, or `update`)
  -> also read `.agents/shared/worker/references/skill-outline-fields.md` (the
  outline-gate contents); for an `update` -> also read
  `.agents/shared/worker/references/update-vs-create-new.md` (in-place vs.
  new-sibling decision). (A committed-origin update skips the design gate, so it
  won't end up using these two -- loading them anyway is harmless.)
- `ARTIFACT` is `service` or `system-interface` -> also read
  `.agents/shared/worker/references/web-frontend-testing.md` (isolated-instance and
  rendered-page rules).

Then follow them. The operation reference is the lifecycle spine -- it owns the
stages, which gates fire and in what order, and the report templates. The
artifact reference is the operation-agnostic description of the thing you are
hardening -- its layout, test mechanics, and isolation rules. Where the operation
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
