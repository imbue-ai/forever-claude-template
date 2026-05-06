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

If you expect this task to be crystallized via `crystallize-task` (the
typical end of the flow), the slug you pick here will also be used as
the `crystallize-task` slug (`$NAME`). Pick something that makes sense
for both -- the worker's `source_artifacts_dir` handoff assumes the
paths match.

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

Before any other work, validate the operations whose failure could prevent the
whole task from working -- specifically those *not fully under your control*:
external API calls, third-party fetches, auth flows. The test: *if this step
fails, can I work around it without abandoning the user's core ask?* If yes
(your own code, a well-defined transform, an LLM model call with a trivial
fallback), it does *not* belong in this validation pass -- testing it just adds
latency without de-risking. If no, validate it now.

For multi-service asks, validate each uncontrolled dependency independently
*first*, then the combined operation.

- Latchkey setup is part of the normal flow, NOT a failure.
- A failure is when setup itself fails, or post-setup calls don't work. On
  failure, surface a specific cause AND propose 1-2 concrete alternatives
  before asking the user to choose.

Keep validation code simple -- inline bash, `uv run python -c`, or short
scripts under `runtime/do-something-new/$SLUG/` if substantive.

## Step 5: Generate and present a small minimum-viable sample

Generate and present a *small* sample (5-10 items, or one representative slice
for non-list outputs) in the user's intended delivery channel -- not the full
production pipeline. The point is a fast feedback gate on shape / tone /
density / layout before any long-running step runs at full scale.

Default presentation: a brief natural-language summary, e.g.

> "Fetched 5 emails: 'Re: Q3 plan' from Alex, 'Lunch?' from Maya, ..."

Save the raw JSON to `runtime/do-something-new/$SLUG/sample.json` so the user
can ask to see it. Pick a different format (table, raw JSON inline, structured
prose) if the data shape or the user's apparent technical preference makes it
more useful.

If the user rejects the sample ("this isn't what I wanted"), go back to Step 3
and re-propose. Re-run Step 2 only if the new ask requires fresh research.

### Sample-first for batch operations

For any step that processes a batch of items (LLM summarization,
transformations across many records, generation calls, large fetches), run it
on a small sample (5-10 items) first. Show the user the shape and tone of the
output and surface measured cost and runtime alongside the sample
("summarizing 5 items took 12s and cost $0.013 -- extrapolated to 150 items,
~$0.40 and ~6 min"). Only scale to the full set after the user thumbs-up.

Sampling is cheap when the operation is fast and load-bearing when it's slow,
so don't try to judge in advance whether a step is "long enough" to need this
-- just apply it by default to any batch step.

## Step 6: Deliver remaining surfaces one at a time

Once the user approves the Step 5 sample, additional surfaces (scheduling,
persistence, history, live integration with a forwarded service, etc.) each
get their *own* delivery and feedback gate. Don't bundle them. Build one,
ship it, ask "want me to add scheduling next, or stop here?", wait, then
build the next.

This applies even when the user's original prompt enumerated several
surfaces -- a single approval on the sample is not blanket approval for the
rest. The user needs to be able to thumbs-up / thumbs-down each surface
independently, which is impossible if four of them land at once.

## Step 7: Crystallize in the background and hand off to interface design

The user's sample-approval at Step 5 IS the explicit go-ahead to
crystallize -- `crystallize-task`'s Step 1 pre-gate question is
suppressed in this case (it names `do-something-new` as a skip
trigger). Do not re-ask.

1. **Kick off `crystallize-task`** with `source_artifacts_dir:
   runtime/do-something-new/$SLUG/`. Its Step 3 includes that
   directory in the task frontmatter and its Step 4 pushes it to the
   worker, so the worker has the scripts and sample data you
   produced.
2. **Launch the lead-proxy poll** (`run_in_background: true`) for
   worker reports, per `crystallize-task` Step 5 /
   `.agents/shared/references/lead-proxy.md`. Do this *before*
   returning to the user. The poll does not block subsequent steps.
   Without it, Gate 1 / Gate 2 reports never reach the user and the
   worker deadlocks waiting for approval.
3. **Hand off to interface design.** Acknowledge that the worker is
   now formalizing the capability, then either follow up on the
   interface the user named in their original prompt (if they did) or
   ask how they'd like to interact with the thing.

The skill's *flow* responsibility ends here; lead-proxy ownership for
the dispatched worker continues until that worker reports terminal
status. Interface design happens in subsequent turns.

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
