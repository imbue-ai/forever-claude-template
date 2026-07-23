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
- To drive the UI manually, launch a **throwaway** instance on an alternate port,
  e.g. `SYSTEM_INTERFACE_PORT=8137 uv run system-interface` from
  `apps/system_interface/`. With `MNGR_HOST_DIR` left at its default it discovers
  the **real** agents (this is how you open the motivating conversation named in
  `## Real scenario` -- see below); point it at fixture data instead when you want
  an isolated, reproducible scene for a committed test.

## Leave a built frontend in your work_dir (required, even for a backend-only change)

Before you report `done`, your work_dir **must** contain a current frontend
build (`cd apps/system_interface/frontend && npm ci && npm run build`, output in
the gitignored `imbue/system_interface/static/`). This is **not** conditional on
whether you touched the frontend: the lead previews your change by booting your
work_dir directly, and the preview **refuses to boot a work_dir with no build**
(it serves the backend's "Frontend not built" placeholder otherwise, which reads
as a broken UI). A fresh worktree has no `node_modules` and no `static/`, so a
backend-only change that skips the build leaves nothing to preview. Build it and
confirm `imbue/system_interface/static/index.html` exists before reporting `done`.

## Real scenario: look at it firsthand, do not imagine it

If the task names a real motivating conversation under `## Real scenario`, **LOOK
AT IT before you touch anything.** You are *not* cut off from that conversation.
Boot your built instance with `MNGR_HOST_DIR` left at its default (see "Running
and testing" above): the system interface then discovers the same real agents the
user sees, so you can drive Playwright (`--no-sandbox`) to the named agent's
conversation and **screenshot the actual thing the user complained about** (use
the tab bar's add-tab `+` dropdown to switch to the agent, or navigate to it
directly). Open the screenshot and study the real rendering. Fix against *that*,
then re-render the same conversation and confirm with your own eyes that it now
looks right. This is the whole point: you see the real case rather than
reconstructing it from the brief.

Only after you have seen and fixed the real case do you crystallize a committed
regression test. A CI test can't depend on a user's conversation existing, so its
fixture is necessarily synthetic -- but shape it from the **real DOM you just
observed** (same element nesting, same classes present), not from imagination,
assert the DOM actually has that shape (so the test can't silently pass against
the wrong tree), and confirm it **fails before your fix and passes after** by
reverting the change. If you genuinely cannot reach the named agent (it isn't
discoverable from your instance), raise a `question` gate rather than falling back
to a guessed fixture.

If the task says there is **no real scenario** (net-new work with no precedent in
any existing conversation), build a representative synthetic fixture as usual --
there is nothing real to point at, so a faithful fixture is correct here.

## Working in isolation

Beyond the live-service rules in `web-frontend-testing.md`: do not run `mngr
start --restart system-services` or `npm run build` against the served tree.
