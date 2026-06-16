# The interactive-delivery shape

The shared skeleton for any task where you build something *for the user to
react to* -- a fetched-and-processed dataset, a web view, a report. It is the
**live half** of "live first, ratify at turn-end": keep the conversation
interactive, get the basic shape confirmed *fast and cheap*, and only then spend
the expensive, thorough effort -- in the background.

This is a **reference, not a skill**. Specializations (`fetch-process-show`,
`build-web-service`) read it for the generic flow and bind its task-specific
hooks. It carries the skeleton and the cross-cutting principles only; it carries
**no concrete question lists** -- those are per-specialization, because the
business-logic questions worth asking differ by task and are not shared.

## Why this shape exists

The failure mode it prevents: an agent works for a long time -- scaffolding,
testing, hardening -- before the user ever confirms the basic thing is right,
then has to throw that work away when the user says "no, not like that." The fix
is to put a cheap, real, throwaway artifact in front of the user *first*, loop on
it until they confirm the shape, and defer everything expensive (thorough tests,
review gates, polish) until after that confirmation -- and run it in the
background so the user is never blocked.

## The skeleton

Each phase names a **hook** the specialization binds. Phases are sequential; do
not skip ahead of an unconfirmed gate.

1. **Clarify only what blocks.** Ask only the questions whose answers change what
   you do next. Zero is fine. Do not gather complete requirements -- the goal is
   to unblock the smallest end-to-end version, not to design the final feature.

2. **Fast feasibility pass.** Time-boxed (a few minutes, a handful of tool
   calls). Enough to not bullshit about whether the task is possible; not a
   research project.

3. **Propose a small plan, wait for approval.** A short plan the user can react
   to -- the approach and what they'll see at the end -- not an exhaustive spec.
   Wait for a go-ahead before building anything.

4. **Validate the risky dependency first** *(hook: validate-risky-dependency)*.
   Before any other work, exercise the operations whose failure could sink the
   whole task and that are *not under your control* (external APIs, auth flows,
   third-party fetches). The test: if this fails, can you work around it without
   abandoning the user's core ask? If yes (your own code, a trivial transform),
   it does not belong in this pass. If no, validate it now. Fail fast here.

5. **Put a cheap, real, throwaway artifact in front of the user**
   *(hook: cheap-throwaway-artifact)*. Produce the smallest *real* artifact that
   lets the user judge the shape, and loop on it: present, take feedback, present
   an updated artifact that *visibly applies* the feedback. Keep this phase
   throwaway -- no production scripts, services, tests, or commits yet. Loop
   until the user **explicitly confirms** the shape is right. That confirmation
   is the gate to everything below.

6. **Hard gate: nothing hardened before confirmation.** Do not crystallize, write
   thorough tests, run review gates, or build production state on an unconfirmed
   artifact. Hardening a moving target bakes in the wrong shape -- and bakes it
   into a background worker that cannot see corrections the user has not made
   yet. The confirmation from phase 5 is what unlocks this.

7. **Harden / ratify** *(hook: harden-ratify)*. The expensive, thorough pass:
   real tests, review gates, polish. This **always runs in a background worker**;
   the main agent never runs the code-guardian gates or the thorough test passes
   itself. Backgrounding never strands the user, because they already have the
   confirmed artifact (or a usable build of it) to work with while the slow
   checks run behind them. The hook is bound to a different background mechanism
   by each consumer (see below) -- the skeleton must not bake in any one.

8. **Deliver further surfaces one at a time.** Each additional surface
   (scheduling, persistence, history, a second view) gets its *own* delivery and
   its *own* feedback gate. Do not bundle them -- a single confirmation on the
   first artifact is not blanket approval for the rest. Build one, ship it, ask
   whether to do the next, wait.

## Cross-cutting principles

- **Demonstrate the UX, elicit the architecture.** The user can only judge the
  visual / interaction shape by *seeing* it, so demonstrate that with a cheap
  artifact. Architecture, by contrast, is something you *elicit and record* --
  covering an architectural dimension means surfacing the decision and writing it
  down, **never building it** during the interactive phase. Decide architecture
  once, at the last responsible moment, from the converged artifact.

- **Default and declare.** Pick the simplest conventional default for any choice
  the user has not constrained, state it in one line, and move on. Only turn a
  choice into a question when it is *both* genuinely uncertain *and* expensive to
  reverse later.

- **Single user by default.** Assume one user (the person you are talking to)
  unless the task clearly implies multiple. Do not build multi-user concerns
  (accounts, sharing, permissions) on speculation.

- **Phrase every choice in business terms, never technical.** This system serves
  non-technical users. Never ask a question the user cannot answer -- "should
  your edits still be here when you come back tomorrow?" not "should we persist
  to a database?"; "should everyone see the same list?" not "do we need
  multi-tenancy?". Translate every architectural fork into the user-visible
  consequence that motivates it.

- **Throwaway until confirmed.** Everything before the confirmation gate is
  disposable by design. Producing the artifact by hand -- you, the agent, doing
  the work in-context -- is fine; what matters is that the *output* is real.
  Polish, scripts, and tests come after, in the hardening phase.

- **Surfaces one at a time.** Never land several deliverables at once; the user
  must be able to react to each independently.

- **Harden in the background; never gate the user on thoroughness.** The main
  agent's job ends at a confirmed, usable artifact. The thorough pass runs in a
  background worker so the user can keep moving.

## Binding the harden-ratify hook

Each consumer binds phase 7 to its own background mechanism. The skeleton stays
mechanism-agnostic:

- `fetch-process-show` binds it to a **`crystallize-task` worker** -- the
  confirmed sample is handed off to be turned into a tested, reviewed pipeline
  skill while the main agent builds surfaces.
- `build-web-service` binds it to a **finalization worker** -- once the user
  confirms the working site, a background worker writes the thorough Playwright
  tests and runs the review gates.

A consumer must not assume any particular mechanism from this doc; it names its
own.
