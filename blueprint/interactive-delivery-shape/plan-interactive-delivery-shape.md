# Plan: shared interactive-delivery shape for all interactive work

> **Refactor the interactive-work skills so all interactive deliverables share one "interactive delivery" shape, fixing the gap where pure web-service tasks (`build-web-service`) get no interactive check-ins.**
>
> * Shared **reference doc** `.agents/shared/references/interactive-delivery.md` holds the generic 8-phase skeleton + cross-cutting principles (demonstrate-the-UX / elicit-the-architecture, default-and-declare, single-user default, throwaway-until-confirmed, surfaces-one-at-a-time, business-terms-not-technical, harden-in-background). Concrete business-logic question sets live per-specialization.
> * `do-something-new` splits: its data fetch-process-show body becomes a new **`fetch-process-show`** skill; `do-something-new` becomes a thin generic net-new dispatcher that points at the shared principles and routes to `fetch-process-show` (data) or `build-web-service` (web view), keeping the existing-skill scan.
> * `build-web-service` gains an interactive front-half (clarify → plan/approve → throwaway mock served as a route inside the normally-scaffolded service → hard gate); the mock renders confirmed `sample.json` data when present. Its existing recipe (scaffold → implement → verify → surface) becomes the build/harden mechanics the skeleton calls into.
> * After mock confirmation, `build-web-service` foregrounds the build-to-a-usable-site (+ smoke verify) so the user can test it themselves, then always self-spawns a background finalization worker for thorough Playwright tests, ratchets, `/autofix`, and the code guardian gates. The main agent never runs the gates.
> * The finalization worker is a bundled worker sub-skill (`build-web-service/assets/worker/SKILL.md`) launched via the existing `--template subskill-worker`, reusing `lead-proxy.md` + `worker-reporting.md`; no reveal/rollback machinery (a new tab needs only `layout.py refresh`).
> * `build-web-service` is invocation-agnostic; the "data need surfaced → message the crystallize worker" responsibility stays in `fetch-process-show`/`crystallize-task`.
> * The confirmed mock is the web flow's single source of truth: if finalization changes the UX shape, re-confirm before done.
> * Scope: the new reference, the `do-something-new` split, the `build-web-service` front-half + worker sub-skill, CLAUDE.md's "Live first, ratify at turn-end" update, and both skills' `description` rewrites. `update-system-interface` is a cousin, left unmodified.

## Overview

- The system already factored the **ratify** half of "live first, ratify at turn-end" into a reusable core (`crystallize-task` / `heal-skill` / `update-skill` sharing `lead-proxy.md` + `worker-reporting.md`). The **live** half has no reusable core — it is trapped inside `do-something-new`, whose body assumes data-fetching and which explicitly excludes "pure dev/code work." This refactor fixes that asymmetry.
- Pure web-service requests today go straight to `build-web-service`, an all-technical recipe (scaffold → implement → verify → surface) with no interactive check-in, so an agent can work for a long time before the user ever confirms the basic shape. This is the concrete bug being fixed.
- Extract the generic interactive-delivery skeleton into one shared **reference** (read, not invoked — matching the existing shared references), and make the consumers thin specializations that bind its task-specific hooks. Two consumers means one shared doc plus two thin references — deliberately not a framework.
- Split `do-something-new` into a thin generic net-new **dispatcher** (keeps the name) and a new data specialization **`fetch-process-show`**; give `build-web-service` the missing interactive front-half; keep both invocation-agnostic where it matters.
- The non-negotiable web-side principle that keeps interactivity fast: **demonstrate the UX, elicit the architecture** — covering an architectural dimension means surfacing/recording the decision (in business terms), never building it; demonstration (mocking) is reserved for the visual/interaction shape the user can only judge by seeing. Architecture is decided once, at the last responsible moment, from a converged mock; default to single-user and the simplest conventional architecture and only ask about forks that are both uncertain and rewrite-forcing.
- The harden/ratify phase always runs in a **background worker**; the main agent never runs the code guardian gates or the thorough test passes. Backgrounding never strands the user because they have the working site to test themselves while the slow/thorough checks run behind them.

## Expected behavior

- **Pure web-service request** ("build me a dashboard"): the agent enters `build-web-service`, asks only blocking clarifications, proposes a small plan, and puts a throwaway mock in front of the user (served as a route inside the scaffolded service) — looping present → feedback → updated mock until the user explicitly confirms the UI shape. Only then does it build the site to a usable state, smoke-verify, and surface it. The user can immediately test it; thorough testing and the guardian gates run in a background worker.
- **Net-new request, no skill applies**: `do-something-new` (now a thin dispatcher) does the existing-skill scan, points the agent at the shared interactivity principles, and routes to `fetch-process-show` (if it involves fetching/processing data) or `build-web-service` (if it is a web view).
- **Data fetch-process-show request**: behaves as `do-something-new`'s data flow does today, now under the name `fetch-process-show` — validate auth/latchkey first, confirm a real `sample.json` covering every data shape, then crystallize in the background while building surfaces.
- **Hybrid (web view over fetched data)**: `fetch-process-show` confirms the data sample first, then drives the surface by delegating to `build-web-service`. `build-web-service` still runs its own interactive UI-mock confirmation (the data sample confirms the data shape, not the UI shape); the only carry-over is the confirmed data, rendered in the mock so the user judges the UI against real data.
- **Architecture questions to the user are always business-logic, never technical** — e.g. "should your edits still be here when you come back tomorrow?" not "should we persist to a database?". Default single-user unless the task clearly implies otherwise.
- **Mock as single source of truth (web)**: if the background finalization changes the UX shape, the user is re-asked to confirm before the work is considered done.
- **No regression in the data flow or in `crystallize-task`** — `fetch-process-show` reuses the same crystallize handoff, lead-proxy poll, and gates; only the skill name and the location of the generic skeleton change.
- **`build-web-service` is invocation-agnostic**: identical behavior whether invoked standalone or by `fetch-process-show`; it never reaches back to a crystallize worker (that coupling lives only in the data flow).

## Changes

### New: `.agents/shared/references/interactive-delivery.md`
- The generic 8-phase skeleton: (1) clarify only what blocks; (2) fast time-boxed feasibility; (3) propose a small plan, wait for approval; (4) validate the risky/uncontrolled dependency first; (5) put a cheap, real, throwaway artifact in front of the user and loop to explicit confirmation; (6) hard gate — nothing hardened before confirmation; (7) harden/ratify, deferred to a background worker; (8) deliver further surfaces one at a time, each with its own feedback gate.
- Cross-cutting principles: demonstrate-the-UX / elicit-the-architecture (covering a dimension = surface/record the decision, not build it); default-and-declare; single-user default; throwaway-until-confirmed; surfaces-one-at-a-time; phrase architecture choices in business terms, never technical; the harden/ratify phase always runs in a background worker and the main agent never runs the guardian gates or thorough test passes.
- Each phase names its task-specific **hooks** (validate-risky-dependency, cheap-throwaway-artifact, harden/ratify) abstractly, so specializations bind them. The reference carries no concrete business-logic question lists — those are per-specialization.
- Note in the doc: the abstract harden/ratify hook is bound to a background mechanism by each consumer (crystallize worker for data; finalization worker for web) — it must not bake in any one mechanism.

### New: `.agents/skills/fetch-process-show/` (extracted from `do-something-new`)
- Move the data-pipeline body of today's `do-something-new` here: latchkey/auth-first validation, the sample loop (`sample.json` covering every data shape, demonstrate-don't-assert, missing-shape handling), the metered-batch cost gate, raw-payload + source capture, the post-confirmation crystallize handoff and lead-proxy poll, "re-fetch while crystallize is running", and the single-source-of-truth surface rules.
- Replace the duplicated generic phrasing with references to `interactive-delivery.md`; keep only the data-specific filling and its concrete business-logic question guidance.
- Reuse `do-something-new`'s slug/runtime conventions; `crystallize-task` continues to receive `source_artifacts_dir` from this skill (rename the path conventions from `runtime/do-something-new/$SLUG/` to `runtime/fetch-process-show/$SLUG/`).
- `metadata.crystallized` stays as appropriate; SKILL.md body must stay ≤ 500 lines.

### Modified: `.agents/skills/do-something-new/SKILL.md` (now the thin dispatcher)
- Strip the data-pipeline body (moved to `fetch-process-show`). Keep: the existing-skill scan (routing), a pointer to `interactive-delivery.md` for the general principles, and routing logic — "fetching/processing data → `fetch-process-show`; a web view → `build-web-service`; otherwise apply the shared principles directly."
- Rewrite the frontmatter `description` so it triggers as a net-new **router**, no longer implying it owns the data flow.
- Note (unresolved wording): the exact routing prose and how explicitly to enumerate the two specializations vs. defer to their own descriptions — settle during implementation, keeping the dispatcher thin.

### Modified: `.agents/skills/build-web-service/SKILL.md` (gains the interactive front-half)
- Prepend an interactive front-half that consumes `interactive-delivery.md`: clarify → plan/approve → throwaway mock (served as a route inside the normally-scaffolded service; scaffolding is fine, building real data/state architecture before confirmation is the stop-and-ask tripwire) → loop to explicit UI-shape confirmation → hard gate. The mock renders confirmed `sample.json` data when present.
- Keep the existing Steps 1-4 (scaffold → implement → verify → surface) as the build/harden mechanics invoked after confirmation; clarify that the post-confirmation foreground work builds the site to a *usable* state plus the existing smoke verify, then stops.
- Add the always-on background finalization handoff: self-spawn the bundled worker sub-skill via `--template subskill-worker` for thorough Playwright testing, ratchets, `/autofix`, and the guardian gates. State in prose that the main agent never runs the gates / thorough passes.
- State that `build-web-service` is invocation-agnostic and that the confirmed mock is the single source of truth (re-confirm if finalization changes the UX shape).
- Add per-specialization business-logic question guidance for web surfacing, phrased as worked examples in business terms plus generate-per-task instruction (not a fixed list).
- Rewrite the frontmatter `description` to advertise the interactive flow (not just "create a new web view").
- SKILL.md body must stay ≤ 500 lines (extraction of the now-shared generic content helps; today it is 336).

### New: `.agents/skills/build-web-service/assets/worker/SKILL.md` (finalization worker sub-skill)
- Bundled worker sub-skill (`metadata.role: worker-sub-skill`) describing the contract: in an isolated worktree, write/extend thorough Playwright tests for the new service, run the full test suite and ratchets, run `/autofix` and the guardian gates, report `done`/`stuck`/`question` per `worker-reporting.md`.
- Explicitly omit reveal/rollback machinery — a new service is just a tab; revealing is `layout.py refresh`, owned by the lead, not life-or-death like `update-system-interface`'s live-UI reveal.
- Launched via the existing `--template subskill-worker` (no new template).
- Note (conditional, per Q&A): if this sub-skill ends up duplicating a lot of text with `update-system-interface`'s worker sub-skill, extract a shared `.agents/shared/references/web-hardening-worker.md` contract that both cite; otherwise keep standalone and revisit only if a third consumer appears.

### Modified: `CLAUDE.md` ("Live first, ratify at turn-end" section)
- Name the live-half core (`interactive-delivery.md`) symmetrically with the ratify-half references, so the doc's stated symmetry is real.
- Update every reference to `do-something-new` to reflect the split (dispatcher vs `fetch-process-show`), including the "do-something-new drives the live phase / hands off to crystallize-task" framing.
- State in writing that the harden/ratify phase runs in a background worker and the main agent does not run the guardian gates (reinforcing the existing stop-hook config, so the main agent won't accidentally start those flows).

### Modified: `.agents/skills/crystallize-task/SKILL.md`
- Update the "if you were invoked from `do-something-new`" wording and the slug/`source_artifacts_dir` convention to point at `fetch-process-show`.

### Repo-wide
- Grep the whole repo for `do-something-new` mentions and fix every stale pointer (skills, references, CLAUDE.md, prose) as part of acceptance.
- `update-system-interface` is left unmodified; note it in the plan/PR as a cousin consumer of the same background-harden principle (validates the design).

### Acceptance
- `uv run .agents/shared/scripts/validate_skill.py <dir>` passes for every new/changed skill (`fetch-process-show`, `do-something-new`, `build-web-service`, and the new worker sub-skill); all SKILL.md bodies ≤ 500 lines.
- End-to-end manual review of the flow prose: a pure-web request, a data request, and a hybrid request each read coherently through the shared reference + their specialization.
- Full repo grep for `do-something-new` returns only intentional, updated references.
- Both rewritten `description`s route correctly (web view → `build-web-service` interactive flow; net-new non-data/non-web → `do-something-new` dispatcher).
