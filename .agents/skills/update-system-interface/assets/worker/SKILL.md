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
  instance on an alternate port against fixture data, e.g.
  `SYSTEM_INTERFACE_PORT=8137 uv run system-interface` from
  `apps/system_interface/` -- a disposable instance, never the live one.

## Testing contract (verify it actually works, then crystallize)

- **If the task file has a `## Real scenario to reproduce` section, your fixture
  MUST reproduce that exact DOM shape -- do not invent a simpler one.** This is
  the most dangerous failure mode for a UI bug fix: you run in an isolated
  worktree and cannot see the conversation/screen that motivated the change, so
  it is tempting to build a plausible-looking fixture from imagination. If that
  fixture's structure differs from the real case even slightly (e.g. prose that
  is a separate trailing message vs. folded into a card), your CSS selector or
  assertion can match your fixture while never matching reality -- and your test
  passes against a structure that does not exist. Build the fixture to match the
  lead-provided structure (same element nesting, same classes present), assert
  the DOM actually has that shape (so the test can't silently pass against the
  wrong tree), and confirm your regression test **fails before your fix and
  passes after** by reverting the change. Reproduce the lead's measured
  before-value and hit the stated target value. If the task gives no real
  scenario but the change is clearly motivated by one, ask the lead for it
  (a `question` gate) rather than guessing.
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
