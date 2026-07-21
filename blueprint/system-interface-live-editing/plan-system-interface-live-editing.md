# Live editing flow for the system interface

Rework the system-interface editing flow so it mirrors `update-service`'s "live loop first, ratify at turn-end" pattern, collapsing the difference onto three system-interface-specific adjustments. Also clarify `update-service`'s mock guidance for all services along the way.

## Overview

- Today `update-system-interface` sends every change to a background worker that must pass the **full** build + test + review gate **before** the user's first preview — so iterating on the workspace UI means waiting out a whole hardening pass per round. This inverts the repo's own "live first, ratify at turn-end" principle.
- The fix: make `update-service` the single front door and add a **system-interface reference** it points to. The system interface becomes "the service that *is* the workspace UI" — the same live-loop-then-harden flow, differing only where it must.
- The difference collapses onto **three adjustments**, each traceable to "a broken build is served straight to the user as their whole workspace": (1) **code isolation** — edit an isolated worktree, never the served tree; (2) the **isolated preview instance is the always-on user's view** (not an escalation, as it is for a normal service); (3) **safe-reveal** (health-checked, auto-rollback) as go-live.
- The live loop is **lead-driven and cheap**: edit worktree → build → preview → iterate with the user; the expensive test/review gate is deferred out of the loop and only runs once, after the user approves.
- Mock guidance is generalized for all services: two distinct demonstrative-artifact types (janky real edit vs. detached prototype), with when-to-use guidance keyed on wiring-cost vs. restart-cost — clarifying that the mock's purpose is **fast feedback**, not just breakage-avoidance.

## Expected behavior

- **Iterating on the workspace UI is fast.** The user sees and reacts to a change after a build (seconds), not after a full harden pass (minutes). The heavy gate runs once, after they're happy.
- **A change is delivered through one lead-driven worktree+preview loop:** edit → build → teardown+reboot the isolated preview → surface. The lead commits before each surface, so branch HEAD always equals the last thing the user saw.
- **The isolated preview is always the user's view during iteration.** Unlike a normal service (where opening a second tab beside an already-open one is awkward), a labeled preview tab reads naturally for the whole-workspace UI. Preview-tab handling is unchanged from today's flow.
- **Every system-interface change runs through the isolated worktree** (code isolation is always required), spun up up-front. Only the preview/mock behavior varies:
  - Visual changes: iterate live in the preview.
  - Backend logic: the preview boots to let the user verify behavior.
  - Test-only / no-surface changes (e.g. a test-suite fix): skip the preview entirely → edit worktree → harden → merge → safe-reveal.
- **How rough the first previewed pass is scales with shape-uncertainty**, not "does it change what the user sees." Obvious contained changes (font, color, reposition, copy) are implemented directly; redesigns / new views / non-obvious layouts start with a deliberately rough pass for fast signal.
- **Two demonstrative-artifact types are available** (for the system interface and for ordinary services):
  - *Type 1 — janky real edit:* rough/hardcoded but in the real code (worktree), shown via the real preview. Faithful; flows straight into the real implementation.
  - *Type 2 — detached prototype:* a separate fake UI. Cheapest/fastest, no real wiring, thrown away; may "not convince" when real rendering matters.
  - Choose by **wiring-cost vs. restart-cost**: Type 2 for fastest look-and-feel / comparing directions when a fake conveys it and real wiring is costly; Type 1 when real context is needed to convince, integration is cheap, or the rough version should flow into the real thing. The embedded system-interface UI defaults to Type 1; Type 2 is reserved for genuinely standalone new surfaces.
- **Handoff to the worker depends on whether the approved artifact is real code yet:**
  - *Type 1 (janky real edit):* hands off directly — the worker's task is "implement this approved shape for real, then harden."
  - *Type 2 (prototype):* the lead first builds it for real in the worktree and does a live-preview round (real context), then hands off to "harden."
- **A final preview-before-merge is kept when the worker produced real work the user hasn't seen** (Type 1 janky → real: worker done → lead previews the worker's output → merge + safe-reveal) **and the change warrants a preview at all** — the system interface always warrants it; other services only sometimes (a tab refresh after go-live often suffices). It is optional when the user already previewed a polished real version.
- **Only one system-interface edit happens at a time:** the editing lease is held across the whole live phase (a deliberate divergence from `update-service`'s per-turn lease release). The worktree/preview persist across turns; if the user never approves, explicit abandonment tears everything down (no idle timeout).
- **Go-live is unchanged:** the health-checked, auto-rollback safe-reveal.

## Changes

- **Make `update-service` the single front door** for the workspace UI, and add a **system-interface reference doc** in its folder describing the three adjustments and the flow. Remove the standalone `update-system-interface` skill.
- **Move `reveal_system_interface.py`** into `update-service`'s folder; it stays largely unchanged (owns `preview` / `unpreview` / `reveal`). The live loop re-uses `preview` by tearing down and rebooting each iteration — no new refresh capability needed.
- **Lead-driven live loop replaces "worker hardens before first preview."** The lead edits the isolated worktree, builds, previews, and iterates with the user; the full test + review gate is deferred until the user approves.
- **Spin up the worker + its worktree up front** for every system-interface change, using the worktree as the lead's live-editing space. Handoff to the worker is deferred: the real harden task is delivered only on approval.
- **Add a "don't send the task on launch" option to `launch-task`'s worker creation**, so a worker can be created (with a frontmatter-only, blank-body task file) and hold its task until the lead delivers the real harden brief later.
- **Define the two worker-handoff shapes:** "implement the approved shape for real + harden" (Type 1 janky), and "harden only" / committed-origin verify (an already-polished or already-real-and-previewed change).
- **Keep the final preview-before-merge conditionally** — gated on both "the worker produced unseen real work" and "this change warrants a preview" (always for the system interface; sometimes for other services).
- **Generalize `update-service`'s mock guidance:** document the two demonstrative-artifact types and the wiring-cost-vs-restart-cost choice, and tighten the wording so the mock's fast-feedback purpose is explicit (not only its fidelity-escalation). Document "does this change warrant a final preview at all?" as a general `update-service` judgment.
- **Hold the editing lease across the whole live phase** for the system interface (no concurrent edits), diverging from `update-service`'s per-turn release; persist worktree/preview across turns; explicit-abandonment teardown only.
- **Update every cross-reference** that points at the standalone `update-system-interface` so it routes through `update-service` + the new reference: CLAUDE.md's routing / skill list, `update-service`'s own "for the workspace UI use update-system-interface" pointer, and any other skill mentioning it.
