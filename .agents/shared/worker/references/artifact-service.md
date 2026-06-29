# Artifact: service

A web service -- a scaffolded Flask lib under `libs/<package>/`, registered in
`supervisord.conf`, reachable at `/service/<name>/` through the system_interface
proxy. This reference describes what a service *is*; for how to run and test a web
frontend in isolation, see
`.agents/shared/worker/references/web-frontend-testing.md`.

## Where the source lives

- The scaffolded lib: `libs/<package>/src/<package>/runner.py` (the Flask app
  and routes), plus its `pyproject.toml`, `README.md`, and
  `test_<package>_ratchets.py`.
- The service entry in `supervisord.conf` and the matching root `pyproject.toml`
  workspace wiring -- you normally do not touch these; the scaffold created them.

## Running and testing

The isolated-instance and rendered-page rules are in `web-frontend-testing.md`.
Service specifics:

- A fresh worktree has no `.venv`, so run `uv sync --all-packages` once before
  any `uv run`. If a fix needs a new dependency, `uv add ...` and commit the
  manifest changes (`pyproject.toml` / `uv.lock`).
- Add a `test_<package>.py` for the routes, and run `cd libs/<package> && uv run
  pytest` (or the repo-root invocation the project uses) plus the ratchets in
  `test_<package>_ratchets.py`.

## Working in isolation

Beyond the live-service rules in `web-frontend-testing.md`: do not run `layout.py
open` / `refresh` / `list` against the served tree, and do not touch
`apps/system_interface` or `libs/web_server/`.
