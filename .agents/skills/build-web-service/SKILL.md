---
name: build-web-service
description: Use when you want to create a new web view for the user. Covers scaffolding a new FastAPI service (canonical path) and the escape hatch for wrapping a pre-existing third-party server, plus diagnostic references when things misbehave.
metadata:
  crystallized: true
---

# How to build a web service

A "web service" here is something the user can click on as a tab in
the desktop client and see render at `/service/<name>/`, proxied
through the workspace_server.

There is one canonical path (scaffold a new FastAPI lib) and one
escape hatch (wrap a pre-existing third-party server). Modify/remove
flows go through the `edit-services` skill.

## Decide which path applies

- **Authoring routes yourself** (the common case): use the FastAPI
  scaffolder in Step 1. The scaffolder picks correct defaults so most
  framework gotchas don't fire.
- **Wrapping a pre-existing third-party server** (Jupyter, Grafana,
  an `npx`-installed dashboard, anything with its own start command):
  skip the scaffolder, jump to "Escape hatch: wrap an existing server"
  below.

If you would otherwise scaffold a FastAPI lib whose only job is to
shell out to a third-party tool, do not do that -- the workspace_server
already proxies `/service/<name>/...` to whatever URL you register.
Adding a Python proxy in front of the third-party server adds a hop,
costs an extra process, and complicates WebSocket and streaming
behavior. Use the escape hatch instead.

Do not extend `libs/web_server/` to add a new view. That lib runs the
top-level workspace UI; new web views go in their own scaffolded lib
under `libs/<your-package>/` so they get an isolated tab and prefix.

## Pre-flight (both paths)

- **Pick a kebab-case service name.** Becomes the URL segment
  `/service/<name>/`. Short and descriptive (`news`, `docs-viewer`)
  beats clever. Avoid names already used in `services.toml`
  (`web`, `system_interface`, etc. are reserved by the scaffolder).
- **Pick a free port.** `ss -tln` lists what's bound. The scaffolder
  picks the lowest free port at or above 8081 by parsing
  `services.toml` and `runtime/applications.toml`; if you're choosing
  manually, avoid `8000` (workspace_server) and `8080` (the example
  `web` service).
- **Bind to `127.0.0.1`** (not `0.0.0.0`). The forwarder reaches your
  app from inside the same container; binding to all interfaces is
  noise. The scaffolder does this. For the wrap-existing path, many
  Node frameworks default to `0.0.0.0` -- pass an explicit host
  (`HOST=127.0.0.1`, `app.listen(port, "127.0.0.1")`, etc.) if your
  third-party tool's default isn't loopback. Python defaults are
  usually loopback already.

## Step 1: Run the scaffolder (canonical path)

```bash
uv run .agents/skills/build-web-service/scripts/scaffold_fastapi_lib.py \
    --name <service-name> \
    --description "<one-liner>" \
    [--port <int>] \
    [--extra-dep <pkg>] [--extra-dep <pkg>] ...
```

Required:
- `--name`: kebab-case (lowercase letters/digits with single hyphens).
- `--description`: becomes the lib `pyproject.toml` description.

Optional:
- `--port`: explicit port; auto-picked if omitted.
- `--extra-dep`: repeatable. Add libraries beyond `fastapi`/`uvicorn`
  (e.g. `--extra-dep "jinja2>=3.1" --extra-dep "anthropic>=0.40"`).
- `--skip-uv-sync`: skip the final `uv sync --all-packages` (for fast
  iteration / dry runs).

The scaffolder fails non-zero with a clear stderr message if the lib
already exists, the name is reserved or invalid, the requested port
is taken, or `uv sync` fails.

What gets generated:

- `libs/<package>/pyproject.toml` -- declares
  `[project.scripts] <name> = "<package>.runner:main"`.
- `libs/<package>/src/<package>/__init__.py` -- empty.
- `libs/<package>/src/<package>/runner.py` -- sync FastAPI starter.
  Reads `ROOT_PATH` from env (default empty) and passes it to
  `FastAPI(...)` so the app emits prefix-aware URLs when reached
  through the proxy.
- `libs/<package>/test_<package>_ratchets.py` -- standard ratchets at
  zero.
- `libs/<package>/README.md` -- one-line description.

What gets updated:

- Root `pyproject.toml` -- adds `<service-name>` to
  `[project].dependencies`, `libs/<package>` to
  `[tool.uv.workspace].members`, and `<service-name> = { workspace = true }`
  to `[tool.uv.sources]`.
- `services.toml` -- inserts:

  ```toml
  [services.<name>]
  command = "ROOT_PATH=/service/<name> python3 scripts/forward_port.py --url http://localhost:<port> --name <name> && uv run <name>"
  restart = "on-failure"
  ```

  The `ROOT_PATH=/service/<name>` prefix is what makes FastAPI emit
  prefix-correct OpenAPI links and absolute redirects when reached
  through the workspace_server. Standalone `uv run <name>` keeps
  working at `/` because the env var is unset there.

The bootstrap service manager picks up the new entry automatically
(no manual restart). Confirm with:

```bash
tmux list-windows | grep "svc-<name>"
```

If the window doesn't appear after a few seconds, capture the bootstrap
window to a file (`tmux capture-pane -t bootstrap -p > /tmp/bootstrap.txt`)
and read it -- do not pipe `tmux capture-pane` through `tail`/`head`,
since CLAUDE.md disallows that.

## Step 2: Implement your routes

The starter `runner.py` has just `GET /` (a placeholder HTML page)
and `GET /health` (returns `{"status": "ok"}`). Replace the
placeholder with your real routes.

Use **sync handlers** (`def`, not `async def`). The starter is fully
sync and most pages don't need otherwise.

### Rendering HTML for a human

If your service renders HTML that a person will look at (anything
beyond a pure JSON API, a webhook receiver, or a transparent proxy of
a third-party tool), you must invoke the `frontend-design` skill **before**
writing the markup. Always do this before working on UI, regardless of the scope of the work.

Skip this step for routes that emit only JSON, only redirects, or that
serve an existing third-party UI through the escape hatch below --
there's no markup to design.

### File-path conventions

Two cases, two patterns:

- **Runtime state files** (caches, cursors, last-visit timestamps,
  JSON snapshots written and read across runs): use cwd-relative
  paths like `Path("runtime/<name>/...")`. The bootstrap-managed
  services run from `/code` (repo root), so this resolves
  consistently. Do NOT use `Path(__file__)`-based paths for runtime
  state.
- **Static assets shipped alongside the .py file** (templates,
  default configs, bundled JSON): `Path(__file__).parent / "assets/..."`
  is the right pattern.

## Step 3: Verify

Both paths use the same verification recipe. See
[references/verify.md](references/verify.md) -- curl against
`http://127.0.0.1:8000/service/<name>/` then a Playwright assertion
on a unique-to-your-app marker.

If verification surfaces something unexpected (502, "duplicated
dockview tab bar", redirect loop, broken WebSockets), see
[references/cross-flow-gotchas.md](references/cross-flow-gotchas.md)
-- it's symptom-indexed.

## Step 4: Surface the view to the user

Once verification passes, tell the workspace UI to actually open the
new tab. Without this step the user would have to discover it via the
"+" dropdown -- skip the surfacing step only for services with no UI
(pure JSON APIs, webhook receivers, etc.).

```bash
python3 scripts/layout.py open <name>
```

`layout.py` POSTs to a loopback-only workspace_server endpoint that
broadcasts a `layout_op` message over its WebSocket. The frontend
focuses the panel if a tab for `<name>` is already open, otherwise
splits a new iframe alongside the primary chat (60% web / 40% chat).
The script briefly waits for the service to appear in
`runtime/applications.toml` so it's safe to run immediately after the
`forward_port.py` call.

To force a reload of an already-open tab (e.g. after redeploying the
service) without prompting the user to click Refresh:

```bash
python3 scripts/layout.py refresh <name>
```

For anything beyond `open` / `refresh` -- splitting, moving, focusing,
renaming, maximizing, replacing an iframe's URL, inspecting the live
tree -- see the `manage-layout` skill. `layout.py list` is also useful
when the user is asking about what tabs are available (it prints every
user-facing registered service plus every mngr-level agent, with
open/running flags; the workspace chrome's own `system_interface` entry
is hidden).

## Escape hatch: wrap an existing server

For pre-existing third-party tools, do not scaffold a lib. Add a
`services.toml` entry that runs `forward_port.py` and then your
existing start command:

```toml
[services.<name>]
command = "python3 scripts/forward_port.py --url http://localhost:<port> --name <name> && <existing_start_command>"
restart = "on-failure"
```

Two valid shapes:

- **Inline** (preferred when one line fits):

  ```toml
  [services.docs-viewer]
  command = "python3 scripts/forward_port.py --url http://localhost:8090 --name docs-viewer && jupyter notebook --port 8090 --ip 127.0.0.1 --no-browser"
  restart = "on-failure"
  ```

- **Wrapper script** (preferred for multi-step bootstrap or env exports):

  ```bash
  # scripts/run_<name>.sh
  #!/usr/bin/env bash
  set -euo pipefail
  python3 scripts/forward_port.py --url http://localhost:<port> --name <name>
  exec <existing_start_command>
  ```

  ```toml
  [services.<name>]
  command = "bash scripts/run_<name>.sh"
  restart = "on-failure"
  ```

The `forward_port.py` call MUST come first in the command -- the port
must be registered before the app starts listening, otherwise the
app-watcher races with the backend coming up.

For schema details on `services.toml`, see the `edit-services` skill.

Verification and gotchas references apply identically to this path.

## `forward_port.py` CLI reference

Used by both paths (the scaffolder generates the call; the escape
hatch has you write it directly).

```
python3 scripts/forward_port.py --name NAME --url URL
python3 scripts/forward_port.py --name NAME --remove
```

Flags:

- `--name`: application name (must match the URL segment a user
  clicks: `/service/<name>/`).
- `--url`: full URL where the app is reachable from inside the
  container (e.g. `http://localhost:8090`).
- `--remove`: remove the named entry from
  `runtime/applications.toml`. Use this when tearing down a service.

## The global (Cloudflare) URL

If the workspace has Cloudflare tunneling configured, the service is
also reachable at a public URL in addition to the local one. Two
caveats:

- **The public hostname is owned server-side**, not by the
  cloudflared process running in this container. Skimming
  `svc-cloudflared`'s tmux output will not surface a URL.
- **The public URL is *not* written into `runtime/applications.toml`.**
  `forward_port.py` only stores `name` and `url` (the local
  `http://localhost:<port>` backend address). Do not grep that file
  for a public URL.

The reliable way to get the public URL is through the desktop client
itself: when the user clicks the service tab, the client resolves the
public hostname via its services API. If you need the exact URL for
testing, ask the user to read it from their browser's address bar.

If the workspace does not have a tunnel token configured, this section
does not apply -- the local `http://127.0.0.1:8000/service/<name>/`
URL is the only entry point.

## Cleanup

Removing a web service:

1. `python3 scripts/forward_port.py --name <name> --remove` (drops the
   entry from `runtime/applications.toml`).
2. Drop `[services.<name>]` from `services.toml` (use `edit-services`
   for guidance on the toml mechanics).
3. If you scaffolded a lib, also: `rm -rf libs/<package>/` and revert
   the matching diff in the root `pyproject.toml` (drop from
   `[project].dependencies`, `[tool.uv.workspace].members`, and
   `[tool.uv.sources]`).
