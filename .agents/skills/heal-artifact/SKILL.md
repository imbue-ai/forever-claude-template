---
name: heal-artifact
description: "Fix an existing artifact that errored or delivered a wrong result. This applies to skills or web services. Invoke at turn-end, after you worked around the failure to satisfy the user's request."
---

# Healing a broken artifact

This is the **heal** lead of the generic artifact lifecycle. An existing
artifact should have delivered the correct result but did not; you dispatch a
generic worker to reproduce the incident, find the root cause, apply a minimal
fix, re-run scenarios, and present a single approval gate. Heal is a turn-end
action -- do not interrupt in-flight work to invoke it; the user's original
request is already delivered.

## The artifact parameter

`artifact` is `skill` (the default) or `service`. The worker reads it and loads
`artifact-<artifact>.md`. (A system-interface regression is a heal *operation*
too, but it is driven through `update-system-interface`, which owns the
`safe-reveal` preview/reveal/rollback go-live -- do not drive a system-interface
heal from here.)

## When NOT to heal

- The artifact worked fine; the request was genuinely out of its scope -- that
  is an `update-artifact` situation, not a heal.
- The failure was one-off and transient (network hiccup, rate limit).
- You are unsure why it failed. Finish the user's request, gather evidence, then
  decide if heal applies.

## Conventions

Use `$TARGET` for the artifact you are healing (e.g. `migrate-config`, a service
name). Then:

- Worker agent name and branch: `heal-$TARGET` / `mngr/heal-$TARGET`
- Runtime dir / task file: `runtime/harden/heal-$TARGET/` /
  `runtime/harden/heal-$TARGET/task.md`

## Step 1: Open a tracking ticket

```bash
mkdir -p runtime/harden/heal-$TARGET
TICKET_ID=$(tk create "heal $TARGET" -t bug \
    --acceptance "task file written; worker launched; worker DONE; branch merged")
tk start "$TICKET_ID"
```

## Step 2: Write the task file

Frontmatter carries `operation: heal`, the `artifact`, and the worker reporting
fields (per `.agents/shared/references/worker-reporting.md`). The body describes
the failure and anchors the worker's search with verbatim quotes (the user's
request, the failing command or error, any tool output that exposed the
misbehavior). Without anchors the worker scans the wrong region of your
transcript.

```bash
cat > runtime/harden/heal-$TARGET/task.md << TASK_EOF
---
lead_agent: $MNGR_AGENT_NAME
finish_report_path: runtime/harden/heal-$TARGET/reports/report.md
operation: heal
artifact: skill
---

# Task: heal \`$TARGET\`

## Incident summary
<2-5 sentences: what the user asked for, how \`$TARGET\` was invoked, how it
failed, what you did to work around it.>

## Anchors (verbatim quotes)
The worker uses these with \`mngr transcript\` to locate the incident. Include
the user's request that invoked \`$TARGET\` (verbatim), the failing output /
exception / wrong result (verbatim), and any clarifying quote about expected
behavior.
<paste quotes here, one per bullet.>

## What the fixed artifact must do
<the contract the healed artifact must honor -- what input shapes should work,
what outputs are correct. Describe success; the incident itself is above.>

## What to do
Use the installed \`harden-worker\` sub-skill. It reads \`operation\` and
\`artifact\` from this frontmatter and follows the matching references:
reproduce the failure, find the root cause, apply a minimal fix, re-run 2-3
fresh scenarios, and push through the single final-artifact gate. Push reports
to the lead per its reporting protocol.

## Success criteria
- The incident reproduces against the current artifact before the fix.
- The fix addresses the root cause, not a symptom.
- The fresh scenarios pass after the fix.
- The user approved the final artifact (via a pushed final-artifact gate report).
TASK_EOF
```

Set `artifact:` as appropriate and fill in the real content; do not leave
placeholders.

## Step 3: Launch the worker and poll

```bash
uv run .agents/skills/launch-task/scripts/create_worker.py launch \
    --name heal-$TARGET \
    --template subskill-worker \
    --runtime-dir runtime/harden/heal-$TARGET/ \
    --task-file runtime/harden/heal-$TARGET/task.md
```

Then background-poll (`create_worker.py await --task-file ... --timeout 90m`,
`run_in_background: true`) and follow `.agents/shared/references/lead-proxy.md`.
Flow-specific substitutions:

- Worker name: `heal-$TARGET`; branch: `mngr/heal-$TARGET`
- Poll path: `runtime/harden/heal-$TARGET/reports/report.md`; reports dir
  `runtime/harden/heal-$TARGET/reports/`; consumed
  `runtime/harden/heal-$TARGET/reports/consumed/`
- The only user-approval gate is `final-artifact` -- a heal has no outline gate.
- Terminal statuses: `done` (go live, Step 4); `stuck` (failure flow per
  `.agents/skills/launch-task/references/worker-failure.md`).

## Step 4: Merge and go live

On `done`, merge `mngr/heal-$TARGET`, then go live by artifact: a **skill** needs
nothing beyond the merge; a **service** wants a tab refresh (`python3
scripts/layout.py refresh <service-name>`). Then close the ticket:

```bash
tk close "$TICKET_ID" "Healed $TARGET -- worker branch merged."
```

## Gotchas

- If the target is a built-in upstream skill, healing it causes local drift to
  reconcile later via `update-self` (pull) or `submit-upstream-changes` (push).
- Heal is non-blocking -- the user's original request is already delivered; the
  heal worker produces a quieter follow-up commit.
