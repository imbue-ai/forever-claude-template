---
name: update-system-interface-worker
description: Implement, build, test, and verify a change to the system interface (apps/system_interface) in an isolated worktree, then report back. Invoke when your task file asks you to make a system-interface UI or backend change.
metadata:
  role: worker-sub-skill
---

# Implementing a system-interface change

Your task file describes a change to `apps/system_interface` -- the web workspace
UI (dockview shell, chat panels, progress view) and/or its Flask backend. You
are running in your **own git worktree** with your own copy of the source, so you
can edit, build, and test in isolation. Nothing you do reaches the user until the
lead merges and reveals your branch, so the bar is: **prove the change actually
works here before you report `done`.**

## Reporting back to the lead

Follow `.agents/shared/references/worker-reporting.md` for the report-file
procedure and the task-file frontmatter schema (`lead_agent` /
`finish_report_path`). Substitutions for this flow:

- `<TASK_FILE_GLOB>` -> `runtime/launch-task/*/task.md`
- `<RUNTIME_REPORTS_DIR>` -> the directory part of `finish_report_path`
  (i.e. `dirname "$FINISH_REPORT_PATH"`).
- Valid `name:` values: `question` (mid-flight gate), `done` / `stuck`
  (terminal).

For a mid-flight `question` gate, stop your turn after pushing -- the lead
replies via `mngr message` and you resume. For terminal statuses, the run ends.

## Where the source lives

- Backend: `apps/system_interface/imbue/system_interface/` (Flask + flask-sock, served by the threaded Werkzeug server).
- Frontend: `apps/system_interface/frontend/src/` (TypeScript + Vite + Tailwind
  + mithril/dockview). Build output goes to the gitignored
  `apps/system_interface/imbue/system_interface/static/`.

## How to run and test it (in-process, never the live service)

- Your fresh worktree has no `.venv` (it is gitignored), so run
  `uv sync --all-packages` once before any `uv run`.
- If your change needs a new dependency, add it the normal way (`uv add` for
  Python, `npm install <pkg>` for the frontend) and **commit the manifest
  changes** (`pyproject.toml` / `uv.lock` / `package.json` / `package-lock.json`)
  on your branch. The lead's reveal detects those manifest changes and refreshes
  the served environment before restarting -- but only if they are committed, so
  they reach the lead in the merge.
- Backend: exercise the edited Python **in-process** -- never install the global
  `system-interface` tool and never touch the running `svc-system_interface`
  service. `cd apps/system_interface && uv run pytest` imports
  `create_application` and exercises it via Flask's test client (and a threaded
  Werkzeug server in-process for the WebSocket/SSE tests), so your edits are
  picked up with no reinstall and no restart.
- Frontend: `cd apps/system_interface/frontend && npm run build` (you must be
  able to produce a clean build) plus `npm run lint` and `npm run test`.
- If you want to drive the UI manually during development, launch a **throwaway**
  instance on an alternate port, e.g. `SYSTEM_INTERFACE_PORT=8137 uv run
  system-interface` from `apps/system_interface/` -- a disposable instance, never
  the live one. With `MNGR_HOST_DIR` left at its default it discovers the **real**
  agents (this is how you open the motivating conversation named in `## Real
  scenario`); point it at fixture data instead when you want an isolated,
  reproducible scene for a committed test.

## Testing contract (verify it actually works, then crystallize)

- **If the task names a real motivating conversation under `## Real scenario`,
  LOOK AT IT before you touch anything -- do not imagine it.** You are *not* cut
  off from that conversation. Boot your built instance with `MNGR_HOST_DIR` left
  at its default (see "How to run" below): the system interface then discovers
  the same real agents the user sees, so you can drive Playwright (`--no-sandbox`)
  to the named agent's conversation and **screenshot the actual thing the user
  complained about** (use the tab bar's add-tab `+` dropdown to switch to the
  agent, or navigate to it directly). Open the screenshot and study the real
  rendering. Fix against *that*, then re-render the same conversation and confirm
  with your own eyes that it now looks right. This is the whole point: you see the
  real case rather than reconstructing it from the brief.

  Only after you have seen and fixed the real case do you crystallize a committed
  regression test. A CI test can't depend on a user's conversation existing, so
  its fixture is necessarily synthetic -- but shape it from the **real DOM you
  just observed** (same element nesting, same classes present), not from
  imagination, assert the DOM actually has that shape (so the test can't silently
  pass against the wrong tree), and confirm it **fails before your fix and passes
  after** by reverting the change. If you genuinely cannot reach the named agent
  (it isn't discoverable from your instance), raise a `question` gate rather than
  falling back to a guessed fixture.
- **If the task says there is no real scenario** (net-new work with no precedent
  in any existing conversation), build a representative synthetic fixture as usual
  -- there is nothing real to point at, so a faithful fixture is correct here.
- **For any change that touches the frontend, you MUST look at the rendered page
  -- not just assert on the DOM.** This is the single most important step and the
  one most easily skipped. A clean build and passing Playwright assertions prove
  the markup and wiring exist; they do NOT prove the page *looks* right -- layout,
  spacing, alignment, overflow/truncation, color/contrast, z-order, and whether
  your change broke something visually elsewhere. So before you report `done`:
  capture screenshots of every page and state your change affects, driving the
  same isolated Playwright instance (e.g. `page.screenshot(path=...)`, and
  `page.set_viewport_size(...)` if layout is width-sensitive), then **actually
  open and view those images and judge them with your own eyes.** Confirm the
  change looks correct and nothing regressed; if anything looks off, fix it and
  re-screenshot until it does. "The tests pass" is never a substitute for having
  looked -- a UI can be visibly broken while every assertion is green. These
  development screenshots are a manual check, not a committed test (do not try to
  assert pixels in CI).
- **Verify the change really works**, driving the UI with Playwright against an
  isolated instance. The existing harness in
  `apps/system_interface/imbue/system_interface/test_e2e.py` already spins up an
  isolated threaded Werkzeug server on an alternate port (`Config(system_interface_port=...)`),
  builds fake agent/session fixtures via `_make_agent_fixture`, and drives it
  with Playwright (auto-skips when browsers aren't installed). Extend it -- and
  use it as the same instance you screenshot for the visual check above.
- For each kind of test, use **exactly one** of crystallized-vs-ad-hoc -- do not
  duplicate the same coverage in both a committed test and a throwaway manual
  check. Crystallize the behavior worth keeping (a Playwright assertion in
  `test_e2e.py`, a backend unit test); use ad-hoc manual checks only for things
  not worth a permanent test (e.g. eyeballing a purely visual tweak).
- Run the suites that apply to the change: backend `pytest`
  (`cd apps/system_interface && uv run pytest`), and for frontend changes
  `npm run lint` + `npm run test`.
- Run the repo's review gates before reporting `done`. Your template already
  enables autofix + CI gates; report `done` only when all tests and gates pass.

## What you must NOT do

- Do not run `mngr start --restart system-services`, restart
  `svc-system_interface`, or `npm run build` against the served tree. Revealing
  the change is the lead's job, after merge -- the lead rebuilds the bundle and
  reloads any open browser (frontend) or restarts the service (backend). Your job
  ends at a committed, verified branch.
