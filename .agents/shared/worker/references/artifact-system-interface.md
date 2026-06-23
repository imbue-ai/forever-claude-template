# Artifact: system interface

`apps/system_interface` -- the live web workspace UI (dockview shell, chat
panels, progress view) and its Flask backend. This reference describes what the
system interface *is*; for how to run and test a web frontend in isolation, see
`.agents/shared/worker/references/web-frontend-testing.md`.

It is what the user is looking at *right now*, so you always work against an
**isolated instance** -- nothing you do reaches the live UI directly.

## Where the source lives

- Backend: `apps/system_interface/imbue/system_interface/` (Flask + flask-sock,
  served by the threaded Werkzeug server).
- Frontend: `apps/system_interface/frontend/src/` (TypeScript + Vite + Tailwind
  + mithril/dockview). Build output goes to the gitignored
  `apps/system_interface/imbue/system_interface/static/`.

## Running and testing

The isolated-instance and rendered-page rules are in `web-frontend-testing.md`.
System-interface specifics:

- A fresh worktree has no `.venv`, so run `uv sync --all-packages` once before
  any `uv run`. If your change needs a new dependency, add it the normal way
  (`uv add` for Python, `npm install <pkg>` for the frontend) and **commit the
  manifest changes** (`pyproject.toml` / `uv.lock` / `package.json` /
  `package-lock.json`).
- Backend: exercise the edited Python **in-process** -- `cd apps/system_interface
  && uv run pytest` imports `create_application` and exercises it via Flask's
  test client (and a threaded Werkzeug server in-process for WebSocket/SSE tests),
  so your edits are picked up with no reinstall and no restart. Never install the
  global `system-interface` tool.
- Frontend: `cd apps/system_interface/frontend && npm run build` (you must
  produce a clean build) plus `npm run lint` and `npm run test`.
- The Playwright harness in
  `apps/system_interface/imbue/system_interface/test_e2e.py` already spins up an
  isolated threaded Werkzeug server on an alternate port, builds fake
  agent/session fixtures via `_make_agent_fixture`, and drives it with Playwright
  (auto-skips when browsers aren't installed). Extend it -- and use it as the
  same instance you screenshot.
- To drive the UI manually, launch a **throwaway** instance on an alternate port
  against fixture data, e.g. `SYSTEM_INTERFACE_PORT=8137 uv run system-interface`
  from `apps/system_interface/`.

## Working in isolation

Beyond the live-service rules in `web-frontend-testing.md`: do not run `mngr
start --restart system-services` or `npm run build` against the served tree.
