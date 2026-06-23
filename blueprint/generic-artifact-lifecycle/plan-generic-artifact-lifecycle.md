# Plan: generic artifact lifecycle (crystallize / update / heal across artifacts)

> **One unified design for the whole "edit live, finalize in background" lifecycle across artifacts — so skills, services, and the system interface all share a generic live half (interactive delivery) and a generic ratify half (crystallize / update / heal).**
>
> ### Live half (interactive delivery)
> * Shared `interactive-delivery.md` reference holds the generic 8-phase skeleton + cross-cutting principles (demonstrate-the-UX / elicit-the-architecture, default-and-declare, single-user, throwaway-until-confirmed, surfaces-one-at-a-time, business-terms, harden-in-background).
> * `do-something-new` is a thin net-new **router**; the data flow lives in `fetch-process-show`; `build-web-service` has an interactive front-half (clarify → plan → throwaway mock → hard gate before any hardening).
>
> ### Ratify half (crystallize / update / heal)
>
> * Three generic operation **leads** — `crystallize-artifact`, `update-artifact`, `heal-artifact` — with the artifact as a parameter; each owns its own gate shape and orchestration loop.
> * Artifacts are exactly **{skill, service, system-interface}**; `fetch-process-show` produces a **skill** (goal: as much as possible just a script), so there is no separate "data pipeline" artifact.
> * "Crystallize" = the **create** operation; shared worker contract renamed `crystallize-artifact.md` → `harden-artifact.md`, cited by all three operations.
> * **Collapse** `crystallize-task` / `update-skill` / `heal-skill` entirely into the generic leads (artifact inferred as skill); their triggers (incl. the crystallize nudge) fold into the leads.
> * `build-web-service` (service) and `fetch-process-show` (data→skill) stay as **live-half wrappers** that hand the confirmed artifact to `crystallize-artifact`; `do-something-new` stays the net-new router.
> * **`op-update` is a single flow with a design-gate toggle** keyed on "is the change already committed?" (committed → skip the gate; emergent → run it), replacing the absorb/verify split — no `update-skill` wrapper. The skill-only update-in-place-vs-split-sibling decision lives in the `artifact-skill` ref.
> * Collapse the 5 worker sub-skills into **one generic `harden-worker`** (composes one op ref × one artifact ref × `harden-artifact.md` from the task file). **Keep the existing `subskill-worker` template + `install_worker_skills.sh` unchanged in spirit** — a single worker source makes it install exactly the one worker and removes the all-workers-everywhere pollution of per-skill bundling.
> * **Split refs by reader-need, not topic:** worker refs (op + artifact, each loaded singly) = how to harden; lead skills = orchestrate + go-live. Go-live is lead-only and artifact-selected; no combined go-live doc.
> * **Scope:** base refactor + `update-artifact` / `heal-artifact` for **services** + fold `update-system-interface` into `update-artifact` (thin wrapper, `go-live=safe-reveal`, reveal script retained). `safe-reveal` stays system-interface-only.
> * **Migration:** delete the unused `detect_crystallization_candidate.py` + test; full repo-grep repoint of all old skill names + doc claims to the generic leads.

## Overview

- The whole system follows **"live first, ratify at turn-end"**: a *live half* keeps the conversation interactive (confirm the basic shape cheaply, defer expensive work) and a *ratify half* runs the thorough, committed, reviewed hardening in a background worker. The live half is the shared `interactive-delivery.md` skeleton plus its specializations (`do-something-new` router, `fetch-process-show` data flow, `build-web-service` front-half); the ratify half is the three operations below.
- The **ratify half** without this design is duplicated three ways: `crystallize-task` / `heal-skill` / `update-skill` each re-implement a near-identical lead loop (ticket → task file → launch → poll → merge) and each carry a bundled worker, yet only the *crystallize* workers share the harden contract while heal and update restate thinner versions. The design factors that into generic, composable pieces and removes the duplication.
- The motivating capability: services and the system interface get the same **update** and **heal** lifecycle as skills. Without it, only skills can be updated/healed through the worker pipeline, while services can only be *created* (`build-web-service`). Generalizing the operations lights up `update-artifact` / `heal-artifact` for services for free, and lets `update-system-interface` become a thin specialization rather than a parallel implementation.
- The structuring principle is **split references by reader-need, not by topic.** A worker hardening one artifact loads exactly one operation ref + one artifact ref + the universal harden contract — never the other cells. Go-live is a lead-only concern selected by artifact, so it never enters worker context, and there is no combined "all strategies" doc that would force an agent to load procedures it won't run.
- The axis of variation is a 2D matrix: **operation** (crystallize / update / heal) owns the gate shape and pre-work; **artifact** (skill / service / system-interface) owns layout, isolation/test mechanics, and which go-live strategy applies. Three generic lead skills hold the operation axis; six composable references hold the worker-facing knowledge; one generic worker composes them per task file.
- The live and ratify halves meet at a clean seam: a live-half wrapper (`build-web-service`, `fetch-process-show`, or a bare net-new task) gets the user to confirm a cheap artifact, then hands that confirmed artifact to a ratify-half lead (`crystallize-artifact` for a first-time create) for the background harden pass. Later changes to an artifact that already exists enter through `update-artifact` or `heal-artifact` directly.

## Expected behavior

- **Net-new task** (live half): `do-something-new` routes — `fetch-process-show` for "go get data, do X, show me", `build-web-service` for "build me a page", or the bare interactive-delivery skeleton otherwise. Each confirms a cheap, real artifact (a data `sample.json`, a throwaway UI mock) and loops on it before anything is hardened.
- **Live → ratify handoff**: once the user confirms the cheap artifact, the live-half wrapper hands it to `crystallize-artifact` to create the committed, tested version in the background. The user is never blocked — they hold the confirmed artifact while the slow pass runs.
- **Crystallize a skill** (the common post-turn case): the user says "crystallize this" (or invokes it), and `crystallize-artifact` runs with the artifact inferred as a skill — the full skill-crystallization behavior (reconstruct from transcript, outline gate, scenario craft, post-merge migration) under the generic lead.
- **Crystallize a service / data view**: `build-web-service` and `fetch-process-show` run their live-half (mock/sample confirmation) on their own, then hand the confirmed artifact to `crystallize-artifact` for the background harden pass. `fetch-process-show`'s artifact is a skill (a script-centric pipeline); `build-web-service`'s is a service.
- **Update any artifact**: `update-artifact` runs one flow. If the change was already discussed-and-committed live, it skips the design gate and just hardens/verifies; if the change is emergent (you worked around a gap), it reconstructs it and runs a design gate first. Works identically for a skill, a service, or the system interface.
- **Heal any artifact**: `heal-artifact` reproduces the incident, finds the root cause, applies a minimal fix, re-runs scenarios, and presents a single approval gate — for a broken skill *or* a broken service *or* the system interface.
- **Update the system interface**: `update-system-interface` still exists as the entry point and still does its pre-merge **preview** and `safe-reveal` (reveal/rollback) go-live, but it now delegates the worker/orchestration core to `update-artifact` with `artifact=system-interface`. The reveal script and the never-touch-the-served-tree guarantees are unchanged.
- **No regression for existing flows**: a skill crystallize/update/heal, a data fetch-process-show, and a web-service build each read coherently and behave as before; only the skill names the user/agent reaches for and the location of shared prose change.
- **Worker context is leaner**: a worker now has exactly one worker sub-skill installed (the generic `harden-worker`) instead of all five, and loads only the one operation ref + one artifact ref relevant to its task.
- **Lead context is unchanged in cost**: the collapsed skill names (`crystallize-task`, etc.) stop auto-loading, replaced by the three generic leads; worker-only material never auto-loads (it lives as references or installed-only worker content).

## Changes

### Live half (interactive delivery)

- `.agents/shared/references/interactive-delivery.md` — the generic 8-phase live skeleton + cross-cutting principles.
- `do-something-new/` — thin net-new router (existing-skill scan, then route to `fetch-process-show` / `build-web-service` / the bare skeleton).
- `fetch-process-show/` — the data flow (auth-first validation, `sample.json` confirmation, raw-payload capture).
- `build-web-service/` — interactive front-half (clarify → plan → throwaway mock served as a route → hard gate) ahead of its scaffold/implement/verify mechanics.
- The ratify-half generalization touches these only at their harden handoff: each hands its confirmed artifact to `crystallize-artifact` + the generic worker (noted below).

### New: generic operation lead skills (`.agents/skills/`)

- `crystallize-artifact/` — generic **create** lead: ticket → task file → launch worker → poll (`lead-proxy.md`) → merge → go-live. Owns the crystallize gate shape (outline gate + final-artifact gate) and the post-merge migration step. Its `description` absorbs `crystallize-task`'s triggers (incl. "crystallize this" and the crystallization-nudge wording) and defaults the artifact to *skill* when invoked standalone post-turn; `build-web-service` / `fetch-process-show` invoke it with the service / skill artifact respectively.
- `update-artifact/` — generic **update** lead. Single flow with a **design-gate toggle** (change already committed → skip Gate 1; emergent change → run Gate 1), replacing absorb/verify. Owns merge + go-live.
- `heal-artifact/` — generic **heal** lead: reproduce → root-cause → minimal fix → single final-artifact gate → merge + go-live.
- Each lead selects, from its artifact parameter, (a) the artifact ref the worker should load, and (b) the go-live strategy to run after merge.

### New: one generic worker

- A single `harden-worker` worker sub-skill replaces the five bundled workers. It reads `{operation, artifact}` from its task file, then follows `harden-artifact.md` (universal) + the named operation ref + the named artifact ref. It owns nothing artifact- or operation-specific itself.
- **Home it under `.agents/shared/worker/`** so no worker-only material lives in the auto-loaded `.agents/skills/` tree, and extend `install_worker_skills.sh` to install the worker sub-skill from that shared path.
- Keep the `subskill-worker` create-template as-is otherwise (role=worker so the Stop hook skips in-worker; the review gates it already enables). With one worker source, every subskill-worker now installs exactly one worker sub-skill instead of all five.

### New: shared references (`.agents/shared/references/`), split by reader-need

- `harden-artifact.md` — **renamed from** `crystallize-artifact.md`; the universal worker harden contract (bar, isolation, reporting, testing, review gates, preserve-and-surface, give-up). Now cited by all three operations.
- `op-crystallize.md`, `op-update.md`, `op-heal.md` — worker-facing operation refs. Each holds only that operation's pre-work + stages: crystallize (reconstruct-from-transcript when the artifact doesn't pre-exist, outline gate, scenario craft); update (the design-gate toggle, the no-change-needed exit); heal (reproduce, root-cause discipline, minimal-fix bound). A worker loads exactly one.
- `artifact-skill.md`, `artifact-service.md`, `artifact-system-interface.md` — worker-facing artifact refs. Each holds only that artifact's layout, isolation/run-in-place recipe, test specifics, and don't-touch list. The skill ref also carries the update-in-place-vs-split-sibling decision (skill-only). A worker loads exactly one.
- No `go-live-strategies.md`. Go-live stays lead-side and per-artifact: `post-crystallize-migration.md` (skills, kept), the reveal script (system-interface), and a couple of inline lines (services).
- `interactive-delivery.md`, `lead-proxy.md`, `worker-reporting.md`, `spec-summary.md`, `transcript-exploration.md`, `update-vs-create-new.md` are unchanged in role (the last is cited from `op-update.md` / `artifact-skill.md`).

### Collapse: existing skill-targeting skills → thin or gone

- Delete `crystallize-task/`, `update-skill/`, `heal-skill/` (skill dirs + their `assets/worker/` and per-flow references). Their behavior is fully covered by the generic leads with `artifact=skill`; their triggers move to the leads' `description`s.
- `build-web-service/` and `fetch-process-show/` stay as live-half wrappers; their `assets/worker/` is removed and their harden handoff points at `crystallize-artifact` + the generic worker.
- `update-system-interface/` stays as the entry point but becomes a thin wrapper over `update-artifact` (`artifact=system-interface`): it keeps `scripts/reveal_system_interface.py`, the pre-merge **preview** step, and `safe-reveal` go-live, but delegates the worker/orchestration core. Its `assets/worker/SKILL.md` collapses into the generic worker + `artifact-system-interface.md`.
- `do-something-new/`, `launch-task/` unchanged (router; worker plumbing).

### Migration and cleanup

- Delete `scripts/detect_crystallization_candidate.py` and `scripts/detect_crystallization_candidate_test.py` — referenced in docs as a Stop-hook nudge but never wired into `.claude/settings.json`.
- Repo-wide grep: repoint every reference to `crystallize-task` / `update-skill` / `heal-skill` (and the `crystallize-artifact.md` reference rename) to the generic leads / `harden-artifact.md` — including `CLAUDE.md`'s lifecycle section, `README.md:38`, the `worker-gates-via-main` spec mentions, and the live-half wrappers.
- `CLAUDE.md` lifecycle section: restate the three operations as `crystallize-artifact` / `update-artifact` / `heal-artifact`, correct the "stop-hook crystallization nudge" wording (it is documentation/manual, not a wired hook), and point at `harden-artifact.md` as the universal worker contract.

### Acceptance

- `validate_skill.py` passes for every new/changed skill (`crystallize-artifact`, `update-artifact`, `heal-artifact`, the live-half wrappers, `update-system-interface`); all SKILL.md bodies within the size limit.
- A worker launched for each (operation × artifact) cell in scope loads only its harden contract + one op ref + one artifact ref, and has exactly one worker sub-skill installed.
- End-to-end prose read-through: skill crystallize/update/heal, service update/heal, data fetch-process-show, web-service build, and system-interface update each read coherently through lead + shared refs.
- Full repo grep for the collapsed skill names and `detect_crystallization_candidate` returns only intentional, updated references (or none).
