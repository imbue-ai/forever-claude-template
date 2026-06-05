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
- **Demonstrate, don't assert.** Every claim about how the data will be
  processed must be *shown* in a sample, never just promised in prose.
- **The confirmed sample is the single source of truth.** It gates
  crystallization, seeds the first surface, and defines the shape the
  pipeline must reproduce. Nothing downstream may invent a second, different
  way of producing the data.
- **Confirm before you build.** No crystallization and no surfaces until the
  user has explicitly confirmed a sample that covers every data shape.

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

## Step 5: The sample loop -- iterate until the user confirms

Produce a *small* sample and put it in front of the user in their intended
delivery channel. The sample is a **feedback instrument, not the product**:
its job is to let the user confirm the process is right *before* you build or
automate anything.

**Keep this phase throwaway.** No production scripts, services, tests, or
commits yet. Producing the sample by hand -- you, the agent, doing the
processing in-context -- is completely fine; what matters is that the *output*
is real. (Polish, scripts, and tests come later, in the crystallized version
and the surfaces.)

**Demonstrate, don't assert.** Every claim about how the data will be
processed must be visible in the sample, never just promised in prose. If you
say "I'll resolve tracking links" or "newsletters become article lists," the
sample must *show* a resolved link and an extracted article list. A sentence
describing what will happen is not a sample.

**Cover every shape, not the first N.** The sample must exercise every
distinct data shape the task involves -- both the kinds the user named (e.g.
"newsletters, action items, GitHub, events" are four *different* processing
paths) and structural edge cases (an empty/edge variant, a malformed or
plain-text-only record, a single-item vs multi-item case). Sample the
*shape-space*; do not just grab the most recent N records, which silently
omits whatever didn't happen to appear recently. **A path not shown is a path
not verified** -- and the newest/most-novel path is usually the riskiest one.

**Missing shape -> flag, demonstrate, invite.** If a shape the task implies
has no real instance available (e.g. the user asked about newsletters but the
inbox currently has none), do NOT silently skip it. Tell the user that path is
unverified, show what your processing *would* produce for it based on your
expectation of the input, and give them the chance to supply a real example
(they may go find one or hand you search criteria). An unflagged missing shape
propagates -- the crystallized pipeline and every surface inherit an untested
path and nobody knows to look.

**Iterate every feedback round.** Each time the user gives feedback, produce
an *updated sample that visibly applies it* and put it back in front of them.
Do not accept feedback and move on having only asserted you'll apply it. Loop
-- present, feedback, updated sample -- until the user **explicitly confirms**
the sample looks right across all shapes. Only that confirmation unlocks
Step 6. If the user rejects the sample outright, go back to Step 3 and
re-propose (re-run Step 2 only if the new ask needs fresh research).

Save the raw sample to `runtime/do-something-new/$SLUG/sample.json` so the
user can ask to see it and so it can **seed the first surface** (Step 7).
Include in each sampled record its **raw payload and a source reference**,
not only the processed fields -- the first surface renders this sample, and
per the preserve-and-surface principle (CLAUDE.md) that surface must be able
to show the raw record and link to its source. If the sample carries only
extracted fields, the raw/source affordance has nothing to point at.
Default presentation is a brief natural-language summary; pick a table /
inline JSON / structured prose if the data or the user's preference makes it
clearer.

### Cost gate for metered batch steps

When the real processing method is a *metered* automated call (an LLM
completion, a paid API) -- as opposed to you doing it in-context -- measure
its cost and runtime on the small sample and report an extrapolation to the
full set before scaling ("classifying 5 items took 12s and cost $0.013 --
extrapolated to 150 items, ~$0.40 and ~6 min"). Only scale after a thumbs-up.
Apply by default to any metered batch step, whether it comes up here or later
in a Step 6/7 surface -- don't pre-judge whether it's "long enough" to need
this.

## Step 6: After confirmation -- crystallize (background) and start surfaces (foreground)

**Hard gate: do not begin this step until the user has explicitly confirmed
the sample (Step 5) across every data shape.** Crystallizing or building a
surface on an unconfirmed process bakes the wrong process into code *and* into
a background worker that cannot see the corrections the user hasn't made yet.
The confirmation is what unlocks everything below. If you are tempted to
crystallize "to save time" while the sample is still changing, don't -- you
will be crystallizing a moving target.

Once the user has confirmed, do all of the following:

1. **Kick off `crystallize-task`** with `source_artifacts_dir:
   runtime/do-something-new/$SLUG/`.
2. **Launch the lead-proxy poll** (`run_in_background: true`) for
   worker reports, per `crystallize-task` Step 5 /
   `.agents/shared/references/lead-proxy.md`. Do this *before*
   returning to the user. The poll does not block subsequent steps.
   Without it, Gate 1 / Gate 2 reports never reach the user and the
   worker deadlocks waiting for approval.
3. **Begin surfaces (Step 7).** The first surface renders the *confirmed
   sample data* directly (see Step 7) -- you do not need to wait for the
   crystallized pipeline to exist, and you must not re-implement the
   processing a second (cheaper) way to feed it. Either follow up on the
   interface the user named in their original prompt, or ask how they'd like
   to interact with the thing.

Crystallization is an essential part of this process: your work up to this point was potentially ad-hoc,
with rounds of revisions and deviations. And any scripts you created (if you created any) to perform fetching or other processing
may not have appropriate testing and have not been code reviewed. And that's fine! Because now you'll delegate that
work to a background agent while, in the meantime, you move on to building other surfaces for the user.

The skill's *flow* responsibility ends here; lead-proxy ownership for
the dispatched worker continues until that worker reports terminal
status. Interface design happens in subsequent turns.

## Step 7: Deliver surfaces -- one at a time, all from the single source of truth

**Single source of truth.** Every surface renders the *confirmed sample data*
(`sample.json`) and, once it lands, the crystallized pipeline's output --
never a parallel re-implementation of the processing. The first version of a
surface is literally `render(sample.json)`: it shows exactly what the user
approved, so there is structurally *no gap* to fill with a cheaper stand-in
(heuristics, regex, a stub classifier). When the crystallized pipeline is
ready, point the surface at its output. **The pipeline may have changed the
shape** -- crystallization is exactly the moment the worker rethinks how the
task should be done, and improving the output schema there is allowed and
expected. So when you swap it in, diff its output against the confirmed
sample: if the shape changed, update the surface to match and **re-confirm the
result with the user** (they signed off on the sample's shape, not the new
one). The rule that stays absolute is the *single source*: a surface renders
the pipeline's output, or until it lands the confirmed sample -- never a
third, parallel re-implementation. **If you ever feel the urge to write a
second, different way of producing the data to feed a surface, stop** -- that
divergence (the surface showing something the user never confirmed) is the
exact bug this rule exists to prevent.

**Surface the raw data and its source from the first version.** Per the
preserve-and-surface principle (CLAUDE.md), the first surface -- not just
the crystallized one -- includes a clean, unprompted affordance to view
each record's raw payload and jump to its source (e.g. "view raw email" /
"open in Gmail"). Build it in now, from the confirmed sample, so the
throwaway first version and the eventual crystallized version agree rather
than the affordance appearing only after crystallization. This depends on
the sample carrying the raw payload + source reference (Step 5). If you are
building the surface as a web view, the `build-web-service` skill covers the
same requirement.

Once the surface is seeded from the confirmed sample, additional surfaces (scheduling,
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
