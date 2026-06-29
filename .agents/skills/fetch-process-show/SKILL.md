---
name: fetch-process-show
description: "Fetch data from somewhere (an external service, an API, a third-party source), process it, and show it to the user -- the \"go get all this stuff, do something to it, and put it in front of me\" task. Use when the ask is to retrieve real data, transform/summarize/classify it, and surface the result. Validates auth first, confirms a real sample covering every data shape, then crystallizes the pipeline in the background while building surfaces."
---

# Fetch, process, and show data

The data specialization of the interactive-delivery shape. The ask is "go get
this data, do something to it, and show me." The goal is to put a real result in
front of the user *fast*, confirm the processing is right against a sample, then
crystallize the pipeline in the background while you build the surface the user
interacts with.

**Read `.agents/shared/references/interactive-delivery.md` first.** It carries
the generic skeleton (the 8 phases) and the cross-cutting principles. This file
only fills in that skeleton's phases for data work. Where a phase below says
"(skeleton phase N)", the generic behavior lives in that reference; this file
adds the data-specific filling.

The non-negotiable data principle on top of the skeleton: **the confirmed sample
is the single source of truth.** It gates crystallization, seeds the first
surface, and defines the shape the pipeline must reproduce. Nothing downstream may
invent a second, different way of producing the data.

## Conventions

Pick a short kebab-case slug `$SLUG` for the task (e.g. `fetch-emails`,
`email-slack-connections`). It is used for:

- Runtime path: `runtime/fetch-process-show/$SLUG/`
- Sample data path: `runtime/fetch-process-show/$SLUG/sample.json`
- Slug passed to `crystallize-artifact` at the end (reused as its `$NAME`)

## Clarify and scope (skeleton phases 1-3)

Ask only what blocks (phase 1). Then a small research pass (phase 2) and a
proposed plan (phase 3), with these data specifics:

For external services, load the `latchkey` skill first (it documents auth flows,
permission requests, and credential handling you need before running any
`latchkey` command), then run:

```bash
latchkey services list --viable
latchkey services info <svc>   # REQUIRED for each obviously-involved service
```

Running `info` is essential: `list --viable` only shows services that *could* be
authenticated (credentials exist *or* a browser auth flow is available); it
doesn't tell you whether the specific service the user needs is already set up.
Run `info` on each involved service to see its actual credential state and whether
you'll need to trigger an auth flow next.

For services not covered by latchkey, do 1-2 web/docs searches. Stop as soon as
you can propose a plausible plan.

Show the plan as two sections (phrase the data choices in business terms per the
skeleton's principles):

```
Auth: <how authentication will work>

Plan:
1. <step>
2. <step>
...

You'll see at the end: <one line on what data you'll show them>
```

For browser auth, add: "Need to launch the latchkey setup flow -- sign in in the
pop-up browser." For `latchkey auth set`, include the exact command for the user
to run. Wait for approval before any further work.

## Validate auth/latchkey first (skeleton phase 4)

The risky dependency for data work is **the data source's auth**.

**If latchkey is involved in any component of the task, authenticate and test it
first -- before anything else.** Even if the latchkey-backed piece is a small
part of a larger pipeline, get it working end-to-end (auth flow completed, a real
API call succeeds) before building anything else. Latchkey auth is the single most
common source of late-stage failure in this flow; failing fast avoids wasted work
on downstream components you'd have to discard if auth turns out unavailable. For
multi-service asks, validate each uncontrolled dependency independently, then the
combined operation.

- Latchkey setup is part of the normal flow, NOT a failure. Follow the `latchkey`
  skill for auth/permission handling.
- A failure is when setup itself fails, or post-setup calls don't work. On
  failure, surface a specific cause AND propose 1-2 concrete alternatives before
  asking the user to choose.

Keep validation code simple -- inline bash, `uv run python -c`, or short scripts
under `runtime/fetch-process-show/$SLUG/` if substantive.

## The sample loop (skeleton phase 5)

The throwaway artifact for data work is a **small real sample** put in front of
the user in their intended delivery channel. It is a *feedback instrument, not
the product*: its job is to let the user confirm the process is right *before*
you build or automate anything. Keep it throwaway -- no production scripts,
services, tests, or commits yet; producing the sample by hand (you, the agent,
doing the processing in-context) is completely fine, as long as the *output* is
real.

**Demonstrate, don't assert.** Every claim about how the data will be processed
must be visible in the sample. If you say "I'll resolve tracking links" or
"newsletters become article lists," the sample must *show* a resolved link and an
extracted article list. A sentence describing what will happen is not a sample.

**Cover every shape, not the first N.** The sample must exercise every distinct
data shape the task involves -- both the kinds the user named (e.g.
"newsletters, action items, GitHub, events" are four *different* processing
paths) and structural edge cases (an empty/edge variant, a malformed or
plain-text-only record, a single-item vs multi-item case). Sample the
*shape-space*; do not just grab the most recent N records, which silently omits
whatever didn't happen to appear recently. **A path not shown is a path not
verified** -- and the newest/most-novel path is usually the riskiest one.

**Missing shape -> flag, demonstrate, invite.** If a shape the task implies has
no real instance available (e.g. the user asked about newsletters but the inbox
currently has none), do NOT silently skip it. Tell the user that path is
unverified, show what your processing *would* produce for it based on your
expectation of the input, and give them the chance to supply a real example. An
unflagged missing shape propagates -- the crystallized pipeline and every surface
inherit an untested path and nobody knows to look.

**Iterate every feedback round.** Each time the user gives feedback, produce an
*updated sample that visibly applies it* and put it back in front of them. Loop
-- present, feedback, updated sample -- until the user **explicitly confirms** the
sample looks right across all shapes. Only that confirmation unlocks the next
phase. If the user rejects the sample outright, go back to the plan and
re-propose (re-run research only if the new ask needs it).

Save the raw sample to `runtime/fetch-process-show/$SLUG/sample.json` so the user
can ask to see it and so it can **seed the first surface**. Include in each
sampled record its **raw payload and a source reference**, not only the processed
fields -- the first surface renders this sample, and per the preserve-and-surface
principle (CLAUDE.md) that surface must be able to show the raw record and link
to its source. If the sample carries only extracted fields, the raw/source
affordance has nothing to point at. Default presentation is a brief
natural-language summary; pick a table / inline JSON / structured prose if the
data or the user's preference makes it clearer.

### Cost gate for metered batch steps

When the real processing method is a *metered* automated call (an LLM completion,
a paid API) -- as opposed to you doing it in-context -- measure its cost and
runtime on the small sample and report an extrapolation to the full set before
scaling ("classifying 5 items took 12s and cost $0.013 -- extrapolated to 150
items, ~$0.40 and ~6 min"). Only scale after a thumbs-up. Apply by default to any
metered batch step, whether it comes up here or later in a surface -- don't
pre-judge whether it's "long enough" to need this.

## After confirmation: crystallize (background) and start surfaces (foreground)

**Hard gate (skeleton phase 6): do not begin this until the user has explicitly
confirmed the sample across every data shape.** Crystallizing or building a
surface on an unconfirmed process bakes the wrong process into code *and* into a
background worker that cannot see the corrections the user hasn't made yet.

Once the user has confirmed, the harden/ratify pass (skeleton phase 7) runs as
a **`crystallize-artifact` worker** (the crystallize operation, artifact = a
script-centric **skill**):

1. **Kick off `crystallize-artifact`** with `artifact=skill` and
   `source_artifacts_dir: runtime/fetch-process-show/$SLUG/` in the task
   frontmatter, reusing `$SLUG` as its `$NAME`.
2. **Launch the lead-proxy poll** (`run_in_background: true`) for worker reports,
   per `crystallize-artifact` Step 5 / `.agents/shared/references/lead-proxy.md`.
   Do this *before* returning to the user. The poll does not block subsequent
   steps. Without it, Gate 1 / Gate 2 reports never reach the user and the worker
   deadlocks waiting for approval.
3. **Begin surfaces.** The first surface renders the *confirmed sample data*
   directly -- you do not need to wait for the crystallized pipeline to exist,
   and you must not re-implement the processing a second (cheaper) way to feed
   it. Either follow up on the interface the user named in their original prompt,
   or ask how they'd like to interact with the thing.

Crystallization is essential: your work up to this point was potentially ad-hoc,
with rounds of revisions, and any scripts you created may lack testing and review.
That's fine -- you now delegate that work to a background agent while you move on
to building surfaces.

The skill's *flow* responsibility ends here; lead-proxy ownership for the
dispatched worker continues until that worker reports terminal status.

## Deliver surfaces -- one at a time, all from the single source of truth (skeleton phase 8)

**Single source of truth.** Every surface renders the *confirmed sample data*
(`sample.json`), and once it lands, the crystallized pipeline's output -- never a
parallel re-implementation. The first surface is literally `render(sample.json)`:
it shows exactly what the user approved, leaving *no gap* to fill with a cheaper
stand-in (heuristics, regex, a stub classifier). When the pipeline is ready, point
the surface at its output -- but crystallization is exactly when the worker may
rethink and improve the output schema, so diff the new output against the
confirmed sample; if the shape changed, update the surface and **re-confirm with
the user**. If you ever feel the urge to write a second way of producing the data
to feed a surface, stop -- that divergence is the bug this rule exists to prevent.

**Surface the raw data and its source from the first version.** Per the
preserve-and-surface principle (CLAUDE.md), the first surface -- not just the
crystallized one -- includes a clean, unprompted affordance to view each record's
raw payload (rendered in its native format -- "view raw email" shows the rendered
email, not HTML source) and jump to its source (e.g. "open in Gmail"). Build it in
quietly from the confirmed sample, so the throwaway and crystallized versions
agree; this depends on the sample carrying the raw payload + source reference.
**If the surface is a web view, use the `build-web-service` skill** -- it runs its
own UI-mock confirmation (the data sample confirms the data *shape*, not the UI
shape) and covers the same raw-data requirement, including rendering untrusted HTML
safely. Hand it the confirmed `sample.json` so the mock renders real data.

Additional surfaces (scheduling, persistence, history, live integration) each get
their *own* delivery and feedback gate -- even when the user's original prompt
enumerated several. A single approval on the sample is not blanket approval for
the rest. Build one, ship it, ask "want me to add scheduling next, or stop
here?", wait, then build the next.

## Re-fetch while crystallize is running

If the user asks to re-fetch while the in-flight crystallize is still running,
just run the manual flow again -- the two are not in conflict.

If the re-fetch reveals something the worker would actually need (wrong endpoint,
missing auth scope, the user changed their mind about which fields matter), send a
short note to the worker:

```bash
mngr message crystallize-$SLUG -m "<short note about what changed>"
```

This keeps the crystallized skill up-to-date with the user's requirements. When
you review crystallization gates, check that the worker incorporated the newer
requirements.

## Background crystallize gates

The standard lead-proxy mechanics for `crystallize-artifact` apply -- nothing
special. By default Gate 1 outline-approval is answered by the lead unless the
worker has a genuine process question, and Gate 2 final-artifact escalates to the
user but is deferred until the user isn't actively working on something else.
