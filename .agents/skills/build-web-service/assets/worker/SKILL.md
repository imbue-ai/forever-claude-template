---
name: build-web-service-worker
description: Harden a freshly built web service (a scaffolded FastAPI lib under libs/) in an isolated worktree -- write thorough Playwright tests, run the full suite and ratchets, run the review gates -- then report back. Invoke when your task file asks you to finalize a newly built web service.
metadata:
  role: worker-sub-skill
---

# Finalizing a freshly built web service

Your task file points at a web service the lead already built and confirmed with
the user in the foreground: a scaffolded FastAPI lib under `libs/<package>/`,
registered in `services.toml`, reachable at `/service/<name>/`. The user has
already signed off on how it looks and works. Your job is the **thorough pass the
lead deliberately deferred**: prove it actually works under test, harden it, and
pass the review gates -- all in your **own git worktree**, so nothing you do
touches the live service until the lead merges.

The bar: **the service is genuinely well-tested and clean before you report
`done`**, not just "it ran once."

## Reporting back to the lead

Follow `.agents/shared/references/worker-reporting.md` for the report-file
procedure and the task-file frontmatter schema (`lead_agent` /
`finish_report_path`). Substitutions for this flow:

- `<TASK_FILE_GLOB>` -> `runtime/launch-task/*/task.md`
- `<RUNTIME_REPORTS_DIR>` -> the directory part of `finish_report_path`
  (i.e. `dirname "$FINISH_REPORT_PATH"`).
- Valid `name:` values: `question` (mid-flight gate), `done` / `stuck`
  (terminal).

For a mid-flight `question` gate, stop your turn after pushing -- the lead replies
via `mngr message` and you resume. For terminal statuses, the run ends.

## Where the source lives

- The scaffolded lib: `libs/<package>/src/<package>/runner.py` (the FastAPI app
  and routes) plus its `pyproject.toml`, `README.md`, and
  `test_<package>_ratchets.py`. Your task file names the exact package and
  service name.
- The service entry in `services.toml` (and the matching root `pyproject.toml`
  workspace wiring). You normally do not need to touch these -- the lead's build
  created them.

## How to run and test it (in-process / isolated, never the live service)

- Your fresh worktree has no `.venv` (it is gitignored), so run
  `uv sync --all-packages` once before any `uv run`.
- If a fix needs a new dependency, add it the normal way (`uv add ...`) and
  **commit the manifest changes** (`pyproject.toml` / `uv.lock`) on your branch so
  they reach the lead in the merge.
- Exercise the app **in-process** -- import the FastAPI app and drive it with
  `fastapi.testclient.TestClient` / `httpx`, or launch a **throwaway** uvicorn
  instance on an alternate port (never `8000`, never the service's live port).
  Never restart or curl the live `svc-<name>` window.
- For browser-level verification, drive Playwright against that isolated instance.
  The `build-web-service` skill's `references/verify.md` describes the
  curl-then-Playwright recipe; adapt it to your isolated port rather than the live
  proxy.

## Testing contract (verify it actually works, then crystallize)

- **Write or extend thorough tests** for the service's real routes -- assert on
  markers that are true if and only if each route behaves correctly (status,
  rendered content, the raw-data/source affordance, empty and overflow states).
  Add a `test_<package>.py` (and Playwright coverage where the value is in the
  rendered UI, not just the JSON).
- Crystallize the behavior worth keeping as committed tests; use ad-hoc manual
  checks only for purely visual things not worth a permanent test. Do not
  duplicate the same coverage in both.
- Run every suite that applies: `cd libs/<package> && uv run pytest` (or the
  repo-root invocation the project uses), plus the ratchets in
  `test_<package>_ratchets.py`.
- Run the repo's review gates before reporting `done`. Your `subskill-worker`
  template already enables autofix + CI + architecture gates; report `done` only
  when all tests and gates pass.

## What you must NOT do

- Do not restart `svc-<name>`, do not run `layout.py open`/`refresh`/`list`
  against the served tree, and do not try to "reveal" your work. Revealing a new
  service is trivial and is the **lead's** job after merge (a tab refresh) -- it
  is not the life-or-death live-UI reveal that `update-system-interface` needs, so
  there is deliberately no reveal/rollback machinery here.
- Do not touch `apps/system_interface` or `libs/web_server/`.
- Your job ends at a committed, verified branch. The lead merges it and refreshes
  the user's tab.

## If you need to give up

If you cannot get the service to a tested, clean state (e.g. a dependency is
unreachable, or a route's intended behavior is underspecified and you cannot
resolve it from the task file), emit a `name: stuck` terminal report (body shape
per `.agents/shared/references/worker-reporting.md`) stating what blocked you and
where the work stands. Do not report `done` on a service whose tests or gates do
not pass.
