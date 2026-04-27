---
name: do-something-new
description: Use when the user asks you to do something net-new -- a task you haven't done before, no existing skill applies, and getting it right will require nontrivial research, exploration, or experimentation. Skip when an applicable single skill already exists or for pure dev/code-writing work.
---

# Doing something new

Use this skill when the user asks for a net-new task that needs research or
experimentation. The goal is to deliver results they care about *fast*, then
crystallize the process in the background while the conversation continues.

**Principles.**

- The user is actively interacting; return results they care about *fast*.
- The user cares about the experience, not technical details.
- Validate the absolute core capability *first*, before any other work. Fail fast.
- Scripts written during this flow can be simple. Polish belongs in the
  crystallized version, not here.

## Conventions

Pick a short kebab-case slug `$SLUG` for the task (e.g. `fetch-emails`,
`email-slack-connections`). It is used for:

- Runtime path: `runtime/do-something-new/$SLUG/`
- Sample data path: `runtime/do-something-new/$SLUG/sample.json`
- Slug passed to `crystallize-task` at the end

## Step 0: Existing-skill scan

Match the user's ask against the descriptions of the existing skills in
`.agents/skills/`. Bail out if a *single* skill covers the *whole* ask:

> "There's already a `<name>` skill for this -- using it instead."

Then invoke that skill and stop. For *partial* matches (a skill covers a subset
of the ask), continue with this skill but reuse the matching skill for the
subset it handles.

## Step 1: Essential clarifications

Ask only what's blocking. Zero questions is fine. Do not gather complete
requirements -- the goal is to unblock the smallest end-to-end version of the
task, not to design the final feature.

## Step 2: Small research pass

Cap: ~2-3 minutes / a handful of tool calls. Goal: don't bullshit about
feasibility, but don't take long.

For external services, run:

```bash
latchkey services list --viable
latchkey services info <svc>   # for any obviously-involved service
```

For services not covered by latchkey, do 1-2 web/docs searches. Stop as soon
as you have enough to propose a plausible plan.

## Step 3: Propose the plan

Show the user a two-section plan:

```
Auth: <how authentication will work>

Plan:
1. <step>
2. <step>
3. <step>
...

You'll see at the end: <one line on what data you'll show them>
```

For browser auth, include this heads-up in the auth section:

> "Need to launch the latchkey setup flow -- sign in in the pop-up browser."

For `latchkey auth set`, include the exact command for the user to run.

Wait for approval before any further work.

## Step 4: Validate the core capability first

Before any other work, validate the absolute minimum capability the task hinges
on. For multi-service asks, validate each service independently *first*, then
the combined operation.

- Latchkey setup is part of the normal flow, NOT a failure.
- A failure is when setup itself fails, or post-setup calls don't work. On
  failure, surface a specific cause AND propose 1-2 concrete alternatives
  before asking the user to choose.

Keep validation code simple -- inline bash, `uv run python -c`, or short
scripts under `runtime/do-something-new/$SLUG/` if substantive.

## Step 5: Generate and present the data sample

Default presentation: a brief natural-language summary, e.g.

> "Fetched 5 emails: 'Re: Q3 plan' from Alex, 'Lunch?' from Maya, ..."

Save the raw JSON to `runtime/do-something-new/$SLUG/sample.json` so the user
can ask to see it. Pick a different format (table, raw JSON inline, structured
prose) if the data shape or the user's apparent technical preference makes it
more useful.

If the user rejects the sample ("this isn't what I wanted"), go back to Step 3
and re-propose. Re-run Step 2 only if the new ask requires fresh research.

## Step 6: Crystallize in the background and hand off to interface design

When the user approves the sample, kick off `crystallize-task` in the
background. Tell `crystallize-task` that the source artifacts directory is
`runtime/do-something-new/$SLUG/`; its Step 3 includes the directory in the
task frontmatter as `source_artifacts_dir` and its Step 4 pushes the directory
to the worker, so the worker has the scripts and sample data you produced.

**The lead is still on the hook for the lead-proxy poll.** Kicking off
`crystallize-task` is *not* fire-and-forget -- the lead must launch the
background poll for worker reports (per `crystallize-task` Step 5 / 
`.agents/shared/references/lead-proxy.md`) *concurrently with* the
interface-design conversation. The poll is a `run_in_background: true` bash
invocation; it does not block subsequent steps. Without it, Gate 1 / Gate 2
reports never reach the user and the worker deadlocks waiting for approval.

Once crystallize is launched and the lead-proxy poll is running in the
background, end this skill with one of these messages:

- If the user's original prompt named a desired interface (e.g. "web
  dashboard", "Slack bot"):

  > "Great, let me convert this into a robust reusable workflow. Let's talk
  > more about the interface you asked for."

- If they didn't:

  > "Great, let me convert this into a robust reusable workflow. Let's talk
  > about how you'd like to interact with this."

The skill's responsibility ends here. Interface design happens in subsequent
turns.

## Re-fetch while crystallize is running

If the user asks to re-fetch while the in-flight crystallize is still running,
just run the manual flow again. The two are not in conflict.

If the re-fetch reveals something the worker would actually need (wrong
endpoint, missing auth scope, the user changed their mind about which fields
matter), send a short note to the worker:

```bash
mngr message crystallize-$SLUG -m "<short note about what changed>"
```

Otherwise stay silent -- no automatic post-every-refetch ping.

## Background crystallize gates

The standard lead-proxy mechanics for `crystallize-task` apply -- nothing
special. By default that means Gate 1 outline-approval is answered by the
lead unless the worker has a genuine process question, and Gate 2
final-artifact escalates to the user but is deferred until the user isn't
actively working on something else.

## When NOT to use this skill

- An applicable single skill already exists -- use it.
- Pure code-writing or dev work (bug fixes, refactors, adding a button). Those
  aren't research/exploration tasks.
