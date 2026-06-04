# Plan: Easy `system_interface` UI updates via a gated delegation flow

## Refined prompt

> **Make `apps/system_interface` UI-updates easy via a canonical, gated delegation flow (not hot-reload), captured as an agent skill.**
>
> * No hot-reload, dev-mode, file-watching, or automatic restarts. The running UI is the user-facing workspace shell, so a broken intermediate state must never be served.
> * **Backend change** takes effect when the lead restarts the services agent: `mngr start --restart system-services` (hardcoded services-agent name). This bounces its tmux session → bootstrap → all services, and the editable-installed (`uv tool install -e`) `.py` source is picked up.
> * **Frontend change** takes effect after `npm run build` (the `static/` bundle is gitignored and built fresh) followed by an agent-issued **full-UI reload**. Existing `scripts/layout.py refresh` only reloads inner iframes/panels — the dockview shell and a rebuilt hashed bundle need a top-level page reload, which no current op does. Add a new broadcast op that triggers `window.top.location.reload()` (which also reloads the child chat iframes).
> * **Canonical flow = mandatory delegation, no exceptions:**
>   1. The lead agent never edits `system_interface` directly.
>   2. The lead delegates the change to a `launch-task` worker (branch `mngr/<name>`).
>   3. The worker implements, builds, and tests it itself — including spinning up an isolated instance driven by Playwright, extending the existing `test_e2e.py` harness (`e2e_server` fixture + `_make_agent_fixture`) — runs `npm run lint` / `npm run test` / backend `pytest`, and runs the repo review gates before reporting `done`. For each test type, exactly one of crystallized-e2e vs ad-hoc-manual is used — no duplicated coverage.
>   4. On a clean `done`, the lead merges the worker's branch, auto-detects FE / BE / both from the merged diff, then rebuilds + issues the reload (FE) and/or restarts the services agent (BE). Only now does the user see the change.
> * Deliverables: (1) a new full-UI reload broadcast op (server handler + frontend handler + a skill-owned client trigger at `.agents/skills/update-system-interface/scripts/reload_interface.py`), (2) a new `update-system-interface` agent skill encoding the delegate → test → merge → restart/refresh flow, (3) an `apps/system_interface/README.md` update.

## Overview

- The friction today is twofold: backend edits need an awkward manual service bounce (the bootstrap manager only reconciles on `services.toml` mtime change), and frontend edits need a manual `npm run build` plus a browser refresh with no command to trigger it — and there is no documented, repeatable flow for any of it.
- We deliberately reject live-reload / watch / dev-mode. Because `system_interface` *is* the user's workspace shell, the design goal is "never serve a half-broken UI," not "iterate in-place fast." Safety beats immediacy.
- The mechanism for that safety is **mandatory delegation**: all `system_interface` changes go through a `launch-task` worker that builds, tests (Playwright against an isolated instance), and passes review gates on its own branch before the lead merges and reveals the change.
- Two missing primitives are added so the lead can reveal a change cleanly: a documented backend restart (`mngr start --restart system-services`) and a new **full-UI reload** broadcast op (the existing `refresh` only reloads inner panels, not the shell).
- The whole flow is crystallized into an `update-system-interface` skill so it is the single canonical path, with the README documenting the two reveal commands.
- The worker needs no separate install of the live tool: it runs the edited code in-process from its own worktree (editable workspace member) via `uv run`, never touching the global `system-interface` tool or the `svc-system_interface` service that serve the live UI.

## Expected behavior

- A user asking the lead to change the `system_interface` UI (frontend or backend) causes the lead to delegate the work to a worker rather than editing directly; the lead's progress view shows it as one delegation step.
- The worker, on its own branch and its own git worktree (the `worker` template does not share the work_dir), makes the change, builds the frontend if touched (`npm run build`), and verifies it actually works by driving an isolated instance with Playwright (reusing the `test_e2e.py` harness), plus running frontend lint/unit tests and backend `pytest`, plus the repo review gates. It reports `done` only when all of that passes.
- The worker exercises the backend entirely in-process: `cd apps/system_interface && uv run pytest` imports `create_application` and runs uvicorn in-process, so the edited `.py` is picked up with no reinstall and no service restart. A fresh worktree has no `.venv` (gitignored), so the worker runs `uv sync --all-packages` once before `uv run`. Any ad-hoc manual instance the worker launches is a throwaway on an alternate port (e.g. `SYSTEM_INTERFACE_PORT=<alt> uv run system-interface`) against fixture data — never the live service.
- The worker never touches the global `system-interface` tool or the `svc-system_interface` service (both pinned to `/mngr/code`, serving the live UI). Those are only acted on by the lead's post-merge reveal step.
- The lead merges the worker's branch only after a clean `done`. A `stuck` report or timeout is surfaced to the user, never silently retried, and nothing is revealed to the user.
- After merging, the lead inspects the merged diff to classify the change:
  - Backend files (`imbue/system_interface/**/*.py`) changed → lead runs `mngr start --restart system-services`; the running UI reconnects on the restarted backend.
  - Frontend files (`frontend/**`) changed → lead runs `npm run build` to regenerate the gitignored `static/` bundle, then runs the reload trigger; the user's open workspace reloads to the new bundle (shell + all child chat iframes).
  - Both changed → lead does both (restart, then build + reload), so the reload lands against the already-restarted backend.
- Running the new reload trigger broadcasts a single op that the connected browser handles by reloading the top-level page; if no browser is connected it is a harmless no-op, consistent with how the existing layout broadcasts behave.
- The existing `layout.py` surface is unchanged — layout arrangement ops stay there; the new reload trigger is a separate, skill-owned script that is not used by other processes.
- The backend restart never kills the lead: the lead (a chat agent) and the services agent are distinct agents sharing one work_dir, so restarting `system-services` leaves the lead running.

## Changes

- **New full-UI reload broadcast op (backend + frontend of `system_interface`):**
  - Add a new op (e.g. `reload_interface`) to the layout broadcast pipeline that the `/api/layout/broadcast` endpoint accepts and relays over the existing WebSocket channel, alongside the current `refresh` op.
  - Add a frontend handler in the dockview shell's `layout_op` switch that responds to the new op with a top-level `window.top.location.reload()`, reloading the shell and, transitively, the child chat iframes. Distinct from `refresh`, which only reloads per-service / per-panel iframes.
- **New skill-owned client trigger:**
  - `.agents/skills/update-system-interface/scripts/reload_interface.py` — POSTs the new op to `/api/layout/broadcast` (reusing the same workspace-base-url / agent-id resolution pattern as `scripts/layout.py`), with a clear stderr note that the broadcast was sent (no observable layout-state change), matching the `refresh` ergonomics. Not added to `layout.py` and not intended for use by other processes.
- **New `update-system-interface` agent skill (`.agents/skills/update-system-interface/SKILL.md`, symlinked into `.claude/skills/`):**
  - States the hard rule: the lead must never edit `system_interface` directly — always delegate via `launch-task`.
  - Specifies the worker brief: where source lives (backend `imbue/system_interface/`, frontend `frontend/src/`), how to build the frontend, and the testing contract — extend the existing `test_e2e.py` Playwright harness for crystallized e2e coverage, run frontend `npm run lint` + `npm run test` and backend `pytest`, and run the repo review gates; for each test type use exactly one of crystallized-e2e vs ad-hoc-manual (no duplicate coverage); report `done` only when all pass.
  - Specifies the worker's run/test environment: it works in its own worktree, runs `uv sync --all-packages` once (fresh worktree has no `.venv`), and exercises the backend in-process via `uv run pytest` / a throwaway `uv run system-interface` on an alternate port — it must not install or restart the live tool/service.
  - Specifies the lead's post-merge steps: merge the worker branch on clean `done`; classify the merged diff as FE / BE / both; for BE run `mngr start --restart system-services`; for FE run `npm run build` then `reload_interface.py`; for both, restart then build + reload.
  - References `launch-task` for the delegation/merge mechanics rather than restating them.
- **Documentation (`apps/system_interface/README.md`):**
  - Document the backend reveal command (`mngr start --restart system-services`) and the new full-UI reload trigger, and point to the `update-system-interface` skill as the canonical update flow.
  - (No cross-reference added to `manage-layout` — the reload trigger is intentionally outside the layout-arrangement surface.)
- **Out of scope (noted, not done here):** implementing the unused `restart = "on-failure"` policy in the bootstrap manager (fixed elsewhere); any live-reload / watch / dev-mode; un-gitignoring the `static/` bundle (worker builds for its own testing, lead rebuilds after merge).
