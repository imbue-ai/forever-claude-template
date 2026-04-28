---
name: build-web-service
description: Stand up a new FastAPI web-service lib with its own ratchet baseline and services.toml entry. Use when the user asks for a web view, page, dashboard, status panel, or any FastAPI/HTTP UI as part of any task. Default to invoking this the moment you've decided "this needs a web view" rather than reaching for `libs/web_server/`.
metadata:
  crystallized: true
---

# Building a new web-service lib

## When to use

When the user asks for a web view, page, dashboard, status panel, or any FastAPI/HTTP UI as part of any task. Default to invoking this skill at the moment you've decided "this needs a web view" rather than reaching for `libs/web_server/`.

## When NOT to use

Do NOT extend `libs/web_server/` to add a new web app. That lib is a deliberate placeholder example with strict zero-baseline ratchets calibrated for a static page. Adding a polling or long-lived service there forces awkward workarounds (threading.Event().wait, sentinel loops, etc.). Use this skill instead -- it stands up a fresh lib with appropriately calibrated ratchets.

## What this skill does

Given a kebab-case service name and a one-line description, the script `scripts/run.py` materializes a working FastAPI lib at `libs/<package>/` (snake_case = kebab → underscore), wires it into the workspace, adds a `[services.<service-name>]` entry to `services.toml`, and runs `uv sync --all-packages`. The result is a service you can immediately start with `uv run <service-name>` and curl on `/health`.

## Step 1: Run the generator

```bash
uv run .agents/skills/build-web-service/scripts/run.py \
    --name <service-name> \
    --description "<one-liner>" \
    [--port <int>] \
    [--extra-dep <pkg>] [--extra-dep <pkg>] ...
```

- `--name`: required, kebab-case (lowercase letters/digits with single hyphens). Reserved names (`web`, `web-server`, `system_interface`, etc.) are rejected.
- `--description`: required, becomes the lib `pyproject.toml` description.
- `--port`: optional; if omitted, the script picks the lowest free port at or above 8081 by parsing `services.toml` and `runtime/applications.toml`.
- `--extra-dep`: optional, repeatable. Add libraries the starter doesn't include (e.g. `--extra-dep "jinja2>=3.1" --extra-dep "anthropic>=0.40"`).

Run from the repo root. The script fails non-zero with a clear stderr message if the lib already exists, the name is reserved, the requested port is taken, or `uv sync` fails.

## Step 2: Implement your routes

The starter `runner.py` has just `GET /` (a placeholder HTML page) and `GET /health` (returns `{"status": "ok"}`). Replace the placeholder with your real routes.

**Use sync handlers** (`def`, not `async def`). The ratchet `check_asyncio_import` is at zero and the starter is fully sync. If your handler genuinely needs async, you'll need to bump the asyncio ratchet baseline and import asyncio explicitly -- but most pages don't need it.

## Step 3: File-path conventions

Two distinct cases, two different patterns:

- **Runtime state files** (anything written and read across runs -- caches, cursors, last-visit timestamps, JSON snapshots): use cwd-relative paths like `Path("runtime/<service-name>/...")`. The bootstrap-managed services run from `/code` (repo root), so this resolves consistently across processes. Do NOT use `Path(__file__)`-based paths for runtime state -- the bug to avoid is one process writing to `/code/runtime/...` while another reads from `/code/libs/<pkg>/runtime/...`.
- **Static assets shipped alongside the .py file** (templates, default configs, bundled JSON): `Path(__file__).parent / "assets/..."` is the right pattern. These are package-relative, not cwd-relative.

## Step 4: Bump ratchet baselines if your code requires it

The starter `test_<package>_ratchets.py` mirrors the placeholder's structure with all baselines at zero. The starter code passes those zeros, so initial generation is clean. Once you start writing real code, if you legitimately need:

- `time.sleep(...)` in a polling loop → bump `test_prevent_time_sleep`
- `while True:` in a long-running loop → bump `test_prevent_while_true`
- `import asyncio` for async handlers / tasks → bump `test_prevent_asyncio_import`
- `import dataclasses` for typed records → bump `test_prevent_dataclasses_import`

...bump the matching `snapshot(0)` to the actual count. Do not contort the code to dodge the regex (e.g. `threading.Event().wait()` instead of `time.sleep`, `while not stop.is_set():` instead of `while True:`) -- that's evading the ratchet, which is worse than the original violation. The ratchets are calibration, not prohibition.

## Step 5: Verify

The bootstrap service manager watches `services.toml` and starts the new entry automatically. To verify:

```bash
curl http://localhost:<port>/health     # should return {"status": "ok"}
```

If the service doesn't come up:

- `tmux list-windows | grep svc-<name>` -- the bootstrap manager spawns one tmux window per entry.
- `tmux capture-pane -p -t svc-<name>` -- show its output / errors.
- If the window exists but is silent (the bootstrap manager occasionally fails to detect a kill cleanly), kill the tmux window itself with `tmux kill-window -t svc-<name>` and let bootstrap respawn it from `services.toml`.

You can also run the service directly without the bootstrap manager: `uv run <service-name>`. This is convenient for fast iteration; just remember the tmux-managed instance is still running on the same port.

## Files generated

- `libs/<package>/pyproject.toml` -- declares `[project.scripts] <name> = "<package>.runner:main"`.
- `libs/<package>/src/<package>/__init__.py` -- empty.
- `libs/<package>/src/<package>/runner.py` -- sync FastAPI starter with `app`, `index()`, `health()`, `main()`.
- `libs/<package>/test_<package>_ratchets.py` -- standard ratchets at zero.
- `libs/<package>/README.md` -- one-line description.

## Files updated

- Root `pyproject.toml` -- adds `<service-name>` to `[project].dependencies`, `libs/<package>` to `[tool.uv.workspace].members`, and `<service-name> = { workspace = true }` to `[tool.uv.sources]`. Idempotent: re-running with already-present entries is a no-op for those fields.
- `services.toml` -- inserts `[services.<name>]` with the canonical `forward_port.py … && uv run <name>` command and `restart = "on-failure"`.
