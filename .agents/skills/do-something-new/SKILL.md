---
name: do-something-new
description: Use immediately when the user asks you to do something net-new -- a task you haven't done before, no existing skill applies, and getting it right will require nontrivial research, exploration, or experimentation. When doing this, give a very short confirmation message to the user's request, then load this immediately before responding further. Your confirmation message shouldn't mention loading the skill. Skip when an applicable single skill already exists or for pure dev/code-writing work.
---

# Doing something new

Use this skill when the user asks for a net-new task that needs research or
experimentation. The goal is to deliver results they care about *fast*, then
crystallize the process in the background while the conversation continues.

**Principles.**

- The user is actively interacting; return results they care about *fast*.
- The user cares about the experience, not technical details.
- Validate dependencies you do not control (external APIs, auth, third-party
  fetches) *first*, before any other work. Fail fast on those.
- Scripts written during this flow can be simple. Polish belongs in the
  crystallized version, not here.

## Conventions

Pick a short kebab-case slug `$SLUG` for the task (e.g. `fetch-emails`,
`email-slack-connections`). It is used for:

- Runtime path: `runtime/do-something-new/$SLUG/`
- Sample data path: `runtime/do-something-new/$SLUG/sample.json`
- Slug passed to `crystallize-task` at the end (reused as its `$NAME`)

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

For external services, load the `latchkey` skill first (it documents auth
flows, permission requests, and credential handling that you need before
running any `latchkey` command), then run:

```bash
latchkey services list --viable
latchkey services info <svc>   # REQUIRED for each obviously-involved service
```

Running `info` on the involved service(s) is essential -- `list --viable`
only shows services that *could* be authenticated (either credentials exist
*or* a browser auth flow is available); it does not tell you whether the
specific service the user needs is already set up. You must run `info` on
each involved service to see the actual current credential state (and to
know whether you'll need to trigger an auth flow in Step 4).

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

## Step 4: Validate uncontrolled dependencies first

Before any other work, validate the operations whose failure could prevent the
whole task from working -- specifically those *not fully under your control*:
external API calls, third-party fetches, auth flows. The test: *if this step
fails, can I work around it without abandoning the user's core ask?* If yes
(your own code, a well-defined transform, an LLM model call with a trivial
fallback), it does *not* belong in this validation pass -- testing it just adds
latency without de-risking. If no, validate it now.

For multi-service asks, validate each uncontrolled dependency independently
*first*, then the combined operation.

**If latchkey is involved in any component of the task, authenticate and test
it first -- before anything else.** Even if the latchkey-backed piece is a
small part of a larger pipeline, get it working end-to-end (auth flow
completed, a real API call succeeds) before building any other component.
Latchkey auth is the single most common source of late-stage failure in this
flow; failing fast on it avoids wasted work on downstream components that
would have to be discarded if auth turns out to be unavailable for that
service.

- Latchkey setup is part of the normal flow, NOT a failure. Follow the
  `latchkey` skill for auth/permission handling -- load it if you haven't
  already.
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

This is especially important when doing anything involving LLMs - don't spend a ton of the user's money without having confirmed they will like the result!

Default presentation: a brief natural-language summary, e.g.

> "Fetched 5 emails: 'Re: Q3 plan' from Alex, 'Lunch?' from Maya, ..."

Save the raw JSON to `runtime/do-something-new/$SLUG/sample.json` so the user
can ask to see it. Pick a different format (table, raw JSON inline, structured
prose) if the data shape or the user's apparent technical preference makes it
more useful.

If the user rejects the sample ("this isn't what I wanted"), go back to Step 3
and re-propose. Re-run Step 2 only if the new ask requires fresh research.

### Sample-first for batch operations

The same gate applies to *any* batch step, not just the Step 5 sample --
LLM summarization, transformations across many records, generation calls,
large fetches, including batch steps that come up later in Step 6
surfaces. Run on the small sample first, then surface measured cost and
runtime alongside it with an extrapolation to the full set
("summarizing 5 items took 12s and cost $0.013 -- extrapolated to 150
items, ~$0.40 and ~6 min"). Only scale to the full set after the user
thumbs-up.

Apply by default to any batch step -- don't try to judge in advance
whether it's "long enough" to need this. Sampling is cheap when the
operation is fast and load-bearing when it's slow.

## Step 6: Crystallize in the background and hand off to interface design

It may take several rounds of iteration before the user is satisfied with the sample.
That's expected, and you should confirm they like it before moving on.
Once it seems like they're reasonably satisfied, you should:

1. **Kick off `crystallize-task`** with `source_artifacts_dir:
   runtime/do-something-new/$SLUG/`.
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

Crystallization is an essential part of this process: your work up to this point was potentially ad-hoc,
with rounds of revisions and deviations. And any scripts you created (if you created any) to perform fetching or other processing
may not have appropriate testing and have not been code reviewed. And that's fine! Because now you'll delegate that
work to a background agent while, in the meantime, you move on to building other surfaces for the user.

The skill's *flow* responsibility ends here; lead-proxy ownership for
the dispatched worker continues until that worker reports terminal
status. Interface design happens in subsequent turns.

## Step 7: Deliver remaining surfaces one at a time

Once the user approves the Step 5 sample and you've kicked off crystallization in the background, additional surfaces (scheduling,
persistence, history, live integration with a forwarded service, etc.) each
get their *own* delivery and feedback gate. Don't bundle them. Build one,
ship it, ask "want me to add scheduling next, or stop here?", wait, then
build the next.

This applies even when the user's original prompt enumerated several
surfaces -- a single approval on the sample is not blanket approval for the
rest. The user needs to be able to thumbs-up / thumbs-down each surface
independently, which is impossible if four of them land at once.

## Re-fetch while crystallize is running

If the user asks to re-fetch while the in-flight crystallize is still running,
just run the manual flow again. The two are not in conflict.

If the re-fetch reveals something the worker would actually need (wrong
endpoint, missing auth scope, the user changed their mind about which fields
matter), send a short note to the worker:

```bash
mngr message crystallize-$SLUG -m "<short note about what changed>"
```

This way the crystallized skill stays up-to-date with the user's requirements.
When you review crystallization gates, you can check to make sure the worker
incorporated the newer requirements in its design.

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
