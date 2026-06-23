# Artifact: service

A web service -- a scaffolded Flask lib under `libs/<package>/`, registered in
`supervisord.conf`, reachable at `/service/<name>/` through the system_interface
proxy. This reference describes what a service *is* and how you run and test one.

## Where the source lives

- The scaffolded lib: `libs/<package>/src/<package>/runner.py` (the Flask app
  and routes), plus its `pyproject.toml`, `README.md`, and
  `test_<package>_ratchets.py`.
- The service entry in `supervisord.conf` and the matching root `pyproject.toml`
  workspace wiring -- you normally do not touch these; the scaffold created them.

## How to run and test it (isolated, never the live service)

- A fresh worktree has no `.venv`, so run `uv sync --all-packages` once before
  any `uv run`.
- If a fix needs a new dependency, add it the normal way (`uv add ...`) and
  commit the manifest changes (`pyproject.toml` / `uv.lock`).
- Exercise the app **in-process** -- drive the Flask app with its test client
  (`app.test_client()`), or launch a **throwaway** threaded Werkzeug server
  (`run_simple(..., threaded=True)`) on an alternate port (never `8000`, never
  the service's live port).
- For browser-level verification, curl the route to confirm it serves, then
  drive Playwright against that same isolated instance (alternate port, not the
  live proxy).

## Testing specifics

- The real routes are what you test -- assert on markers true if and only if
  each route behaves correctly (status, rendered content, the raw-data/source
  affordance, empty and overflow states). Add a `test_<package>.py`, plus
  Playwright coverage where the value is in the rendered UI, not just the JSON.
- Run every suite that applies: `cd libs/<package> && uv run pytest` (or the
  repo-root invocation the project uses), plus the ratchets in
  `test_<package>_ratchets.py`.

## Working in isolation

- Never restart or curl the **live** service, never run `layout.py
  open`/`refresh`/`list` against the served tree, and never try to "reveal" your
  work -- you only ever drive a throwaway instance on an alternate port.
- Do not touch `apps/system_interface` or `libs/web_server/`.
