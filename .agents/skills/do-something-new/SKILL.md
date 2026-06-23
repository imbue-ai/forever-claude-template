---
name: do-something-new
description: Use immediately when the user asks you to do something net-new -- a task you haven't done before, no existing skill or service applies, and getting it right will require nontrivial research, exploration, or experimentation. Routes the request to the right interactive flow. When doing this, give a very short confirmation message to the user's request, then load this immediately before responding further. Your confirmation message shouldn't mention loading the skill. Skip when an applicable skill or service already exists or for pure dev/code-writing work.
---

# Doing something new

A net-new task that needs research or experimentation has landed. This skill is a
thin **router**: it scans for an existing skill, then sends you to the right
interactive flow. The actual flow lives in the specialization you route to (or,
for the rare task that fits neither, in the shared principles).

The overriding goal of any net-new interactive task is the same: deliver a result
the user cares about *fast*, confirm the basic shape cheaply, and defer the
expensive thorough work to the background. That shape is documented once in
`.agents/shared/references/interactive-delivery.md` -- **read it**; every route
below is a specialization of it.

## Step 0: Existing-skill scan

Match the user's ask against the descriptions of the existing skills in
`.agents/skills/`. If a *single* skill covers the *whole* ask, bail out:

> "There's already a `<name>` skill for this -- using it instead."

Then invoke that skill and stop. For *partial* matches (a skill covers a subset
of the ask), continue routing but reuse the matching skill for the subset it
handles.

## Step 1: Route

Pick the flow that fits the ask:

- **Fetch / process / show data** -- the ask is "go get this data (from an
  external service, API, or third-party source), do something to it, and show me
  the result." Route to **`fetch-process-show`**.

- **Build a web view** -- the ask is "build me a page / dashboard / app I can
  look at." Route to **`build-web-service`**. It owns the interactive
  mock-confirmation flow for web work.

- **A hybrid: a web view over fetched data** -- start with `fetch-process-show`
  to confirm the data sample, then it hands the surface to `build-web-service`
  (which runs its own UI-mock confirmation on top of the confirmed data). Begin
  with `fetch-process-show`.

- **Neither cleanly applies** -- the task is genuinely net-new and not a data
  fetch or a web view. Apply the interactive-delivery skeleton directly from
  `.agents/shared/references/interactive-delivery.md`: clarify only what blocks,
  a fast feasibility pass, a small plan, validate the risky dependency first, a
  cheap throwaway artifact looped to explicit confirmation, then hardening in the
  background. Fill in the skeleton's phases with the specifics of the task at hand.

Load the routed skill and proceed; do not re-implement its flow here.

## When NOT to use this skill

- An applicable single skill already exists -- use it.
- Pure code-writing or dev work (bug fixes, refactors, adding a button). Those
  aren't research/exploration tasks.
