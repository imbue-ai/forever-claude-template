---
name: update-system-interface
description: Canonical flow for changing the system interface (the web workspace UI at apps/system_interface) -- its frontend (dockview shell, chat rendering, progress view) or backend (FastAPI server, agent discovery, layout ops). Use whenever the user wants to edit, fix, restyle, or add to the workspace UI / chat interface / dockview. Enforces delegating the change to a tested worker and revealing it to the user only after it is verified.
---

# Updating the system interface

`apps/system_interface` is the live web UI the user is looking at right now
(the dockview shell, the chat panels, the progress view). Because a broken
build is served straight to the user, **every** change goes through a tested
worker and is revealed only after it passes. This skill is the single canonical
path for that.

## The hard rule

**Never edit `apps/system_interface` directly as the lead agent.** Do not run
`Edit`/`Write` on files under `apps/system_interface/` yourself, and do not
rebuild or restart the live UI from uncommitted lead-side edits. All changes are
made by a `launch-task` worker on its own branch, tested there, and merged back
before anything reaches the user. The only things the lead does to
`system_interface` are the post-merge reveal commands at the end of this skill.

## Flow overview

1. **Delegate** the change to a worker via the `launch-task` skill.
2. The **worker** implements + builds + tests it on its own branch (`mngr/<name>`),
   then reports `done`.
3. The lead **merges** the worker's branch on a clean `done`.
4. The lead **reveals** the change: restart the backend (BE) and/or rebuild +
   reload the frontend (FE), based on what the merged diff touched.

## 1-2. Delegate to a worker

Follow the `launch-task` skill for the mechanics (task file, `create_worker.py
launch`, background-poll the report, handle `done`/`stuck`). This skill only
specifies what to put in the task brief and how to handle the result.

The worker runs in its **own git worktree** (the `worker` template does not
share the lead's work_dir), so it has its own copy of the source to edit and
test in isolation. Put the following in the task file's `## What to do` /
`## Context` / `## Success criteria`:

### Where the source lives
- Backend: `apps/system_interface/imbue/system_interface/` (FastAPI + uvicorn).
- Frontend: `apps/system_interface/frontend/src/` (TypeScript + Vite + Tailwind
  + mithril/dockview). Build output goes to the gitignored
  `apps/system_interface/imbue/system_interface/static/`.

### How the worker runs and tests it (in-process, never the live service)
- The fresh worktree has no `.venv` (it is gitignored), so the worker runs
  `uv sync --all-packages` once before any `uv run`.
- Backend: the worker exercises the edited Python **in-process** -- it never
  installs the global `system-interface` tool or touches the running
  `svc-system_interface` service. `cd apps/system_interface && uv run pytest`
  imports `create_application` and runs uvicorn in-process, so edits are picked
  up with no reinstall and no restart.
- Frontend: `cd apps/system_interface/frontend && npm run build` (the worker
  must be able to produce a clean build) plus `npm run lint` and `npm run test`.
- If the worker wants to drive the UI manually during development, it launches a
  **throwaway** instance on an alternate port against fixture data, e.g.
  `SYSTEM_INTERFACE_PORT=8137 uv run system-interface` from
  `apps/system_interface/` -- a disposable instance, never the live one.

### Testing contract (verify it actually works, then crystallize)
- The worker must **verify the change really works**, driving the UI with
  Playwright against an isolated instance. The existing harness in
  `apps/system_interface/imbue/system_interface/test_e2e.py` already spins up an
  isolated uvicorn instance on an alternate port (`Config(system_interface_port=...)`),
  builds fake agent/session fixtures via `_make_agent_fixture`, and drives it
  with Playwright (auto-skips when browsers aren't installed). Extend it.
- For each kind of test, use **exactly one** of crystallized-vs-ad-hoc -- do not
  duplicate the same coverage in both a committed test and a throwaway manual
  check. Crystallize the behavior that is worth keeping (a Playwright assertion
  in `test_e2e.py`, a backend unit test); use ad-hoc manual checks only for
  things not worth a permanent test (e.g. eyeballing a purely visual tweak).
- Run the suites that apply to the change: backend `pytest`
  (`cd apps/system_interface && uv run pytest`), and for frontend changes
  `npm run lint` + `npm run test`.
- The worker runs the repo's review gates before reporting `done`. The `worker`
  template already enables autofix + CI gates; the worker must report `done`
  only when all tests and gates pass.

### What the worker must NOT do
- Must not run `npm run build` against the live tree, restart
  `svc-system_interface`, or run `reload_interface.py` -- revealing the change is
  the lead's job, after merge.

## 3. Merge on a clean `done`

Handle the worker's report per `launch-task` (its `## 4` and the referenced
`lead-proxy.md`). On terminal `done`, merge the worker's branch (`mngr/<name>`)
into the working branch the live UI is served from. On `stuck` or a timeout with
a dead worker, surface to the user per `launch-task`'s failure flow -- **do not**
reveal anything and do not retry silently.

Note: the built `static/` bundle is gitignored, so the merge brings only
`frontend/src/` changes, not the worker's build output. The lead rebuilds in the
next step.

## 4. Reveal the change (lead only, after merge)

Inspect the merged diff to classify what changed, then run the matching reveal
command(s). Detect by path:

- **Backend** -- any `apps/system_interface/imbue/system_interface/**/*.py`
  changed:
  ```bash
  mngr start --restart system-services
  ```
  This cleanly restarts the services agent (its tmux session -> bootstrap -> all
  services). The editable-installed `system-interface` picks up the merged `.py`
  source. This does not kill the lead: the lead (a chat agent) and the services
  agent are distinct agents sharing one work_dir.

- **Frontend** -- any `apps/system_interface/frontend/**` changed:
  ```bash
  cd apps/system_interface/frontend && npm run build
  python3 .agents/skills/update-system-interface/scripts/reload_interface.py
  ```
  `npm run build` regenerates the gitignored `static/` bundle in the live tree;
  `reload_interface.py` broadcasts a full-UI reload so the user's open workspace
  reloads the new bundle (shell + all child chat iframes). It is a no-op if no
  browser is connected.

- **Both** changed: restart first, then build + reload, so the reload lands
  against the already-restarted backend.

`reload_interface.py` is distinct from `scripts/layout.py refresh` (which only
reloads inner iframes/panels). Use `reload_interface.py` for a system-interface
frontend change; `layout.py` remains for arranging panels.

## Why this shape

The UI is what the user is actively looking at, so the design goal is "never
serve a half-broken UI," not "iterate in place fast." The worker's isolated
worktree + in-process testing + Playwright verification + review gates are what
make it safe for the lead to merge and reveal in one motion.
