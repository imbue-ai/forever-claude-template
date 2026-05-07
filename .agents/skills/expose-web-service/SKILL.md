---
name: expose-web-service
description: End-to-end recipe for exposing an HTTP application as a clickable tab in the Minds desktop client. Composes forward-port (port registration) and edit-services (services.toml mechanics), and adds the framework binding/redirect gotchas plus a verification step. Use when you have an HTTP-speaking app (FastAPI, Flask, Express, static server, etc.) that the user should be able to open in the desktop client at /service/<name>/.
metadata:
  crystallized: true
---

# How to expose a web service

Use this when the user wants to *click on a tab* in the Minds desktop
client and see your application render. The tab loads
`/service/<name>/` from the workspace_server, which proxies to a
backend you register via `scripts/forward_port.py`.

This skill is a recipe. The mechanical primitives are owned by two
sibling skills -- do not duplicate their content:

- `forward-port` -- the `scripts/forward_port.py` invocation and the
  `runtime/applications.toml` contract.
- `edit-services` -- the `services.toml` schema and how the bootstrap
  manager reconciles tmux windows.

What this skill adds: the *composition*, the framework gotchas that
otherwise produce 502s or "I see the chat tab again with a duplicated
dockview tab bar", and a concrete verification step that goes beyond
`curl -I`.

The proven reference pattern is the `[services.web]` entry in
`services.toml`:

```toml
[services.web]
command = "python3 scripts/forward_port.py --url http://localhost:8080 --name web && uv run web-server"
restart = "on-failure"
```

Every step below works toward producing an entry that follows that
shape.

## Step 1: Pre-flight

Before editing anything:

- **Bind your app to `127.0.0.1` (or `localhost`), not `0.0.0.0`.**
  The forwarder reaches it from inside the same container; binding
  to all interfaces is unnecessary and noisy. Many Node frameworks
  default to all-interfaces (Node's `http.createServer().listen(port)`
  binds to `::`/`0.0.0.0` when no host is passed), so pass an explicit
  host (`HOST=127.0.0.1`, `app.listen(port, "127.0.0.1")`, etc.) if
  your server's default is not loopback. Python defaults are usually
  loopback already (e.g. `flask run` defaults to `127.0.0.1:5000`).
- **Pick a free port** the app will listen on. `ss -tln` lists what's
  bound. Avoid the well-known service ports already used in this
  template:
  - `8000`: workspace_server (system_interface)
  - `8080`: web (the example service)
  - Anything else listed in `services.toml` as a `--url
    http://localhost:<port>` argument.
- **Pick a kebab-case service name.** It becomes the URL segment
  `/service/<name>/`. Short and descriptive (`news`, `docs-viewer`)
  beats clever.

## Step 2: Decide inline command vs. wrapper script

Two valid shapes:

**Inline** (preferred when the start command fits one line and needs
no setup):

```toml
[services.<name>]
command = "python3 scripts/forward_port.py --url http://localhost:<port> --name <name> && <start_command>"
restart = "on-failure"
```

**Wrapper script** (preferred when you need env exports, a multi-step
bootstrap, or `uv run --with <pkg>` invocations that get long):

```bash
# scripts/run_<name>.sh
#!/usr/bin/env bash
set -euo pipefail
python3 scripts/forward_port.py --url http://localhost:<port> --name <name>
exec <start_command>
```

```toml
[services.<name>]
command = "bash scripts/run_<name>.sh"
restart = "on-failure"
```

Either way, the **first** step inside the command must be the
`forward_port.py` call so the port is registered before the app
starts listening (otherwise the app-watcher races with the backend
coming up). The example `[services.web]` and `[services.system_interface]`
both follow this order.

## Step 3: Add the entry to `services.toml`

Use the `edit-services` skill for schema details. Copy the
`[services.web]` shape verbatim and substitute name, port, and
command. Set `restart = "on-failure"` for any long-lived server.

## Step 4: Wait for the bootstrap manager to pick up the change

No manual restart needed. The bootstrap service manager watches
`services.toml` and reconciles tmux windows automatically. It also
detects command *changes* (not just additions/removals) and recreates
the window.

Confirm it picked up the new service:

```bash
tmux list-windows | grep "svc-<name>"
```

If `svc-<name>` does not appear after a few seconds, check the
bootstrap window itself for errors:

```bash
tmux capture-pane -t bootstrap -p | tail -40
```

## Step 5: Verify locally with curl

```bash
curl -sf http://127.0.0.1:8000/service/<name>/ -o /dev/null -w "%{http_code}\n"
```

Port 8000 is the workspace_server; it proxies `/service/<name>/...`
to the URL registered in `runtime/applications.toml`. Expected:
`200`.

Common failures:

- **502** -- backend not reachable. Either the app crashed (check
  `tmux capture-pane -t svc-<name> -p`) or it's bound to the wrong
  host (re-check Step 1).
- **404 from workspace_server** -- the service name is not in
  `runtime/applications.toml`. Either `forward_port.py` was not run,
  or it was passed the wrong `--name`.
- **200 but the rendered page is the agent chat with a duplicated
  dockview tab bar** -- this is the same-origin HTML being served
  back instead of your app. It usually means the workspace_server
  could not reach your backend and is falling back. Re-check the
  port and the bind host.

## Step 6: Verify with a browser-equivalent

Curl alone misses iframe-rendering bugs. Use Playwright (preinstalled
in the root venv per `CLAUDE.md`):

```python
# /tmp/verify_<name>.py
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    page.goto("http://127.0.0.1:8000/service/<name>/", wait_until="networkidle")
    title = page.title()
    body = page.content()
    print("title:", title)
    print("body len:", len(body))
    assert "<your-expected-marker>" in body, body[:500]
    browser.close()
```

Run with `uv run python /tmp/verify_<name>.py`. Pick a marker that
*only* appears when your app rendered correctly (a heading, a
data-driven element). Do not assert on `<html>` or `<body>` -- those
appear in error pages too.

## Step 7: Verify the global URL (if applicable)

If the workspace has Cloudflare tunneling configured, the service is
also reachable at a public URL in addition to the local one. Two
caveats matter for verification:

- **The public hostname is owned server-side, not by the cloudflared
  process running in this container.** The `cloudflared` service here
  runs `cloudflared tunnel run --token <TOKEN>` (a named/preauthenticated
  tunnel), which does not print a public URL on stdout. The hostname
  is constructed by `remote_service_connector` as
  `<service>--<agent>--<user>.<domain>` and registered with Cloudflare's
  config-service API. Skimming `svc-cloudflared`'s tmux output will
  not surface a URL -- do not look there.
- **The public URL is *not* written into `runtime/applications.toml`.**
  `scripts/forward_port.py` only stores `name` and `url` (the local
  `http://localhost:<port>` backend address). Do not grep that file for
  a public URL either.

The reliable way to obtain the public URL is through the Minds
desktop client itself: when the user clicks the service tab, the
client resolves the public hostname via its services API and renders
the page at `https://<service>--<agent>--<user>.<domain>/`. If you
need the exact URL for testing, ask the user to read it from their
browser's address bar after clicking the tab.

If the workspace does not have a tunnel token configured, this step
does not apply -- the local `http://127.0.0.1:8000/service/<name>/`
URL from Step 5 is the only entry point.

## Step 8: Framework gotchas

If verification surfaced something unexpected, see
[references/gotchas.md](references/gotchas.md) for framework-specific
traps (uvicorn root_path, FastAPI absolute redirects and the
Location-header rewrite, static-file servers and trailing slashes,
WebSockets).

## Cleanup

If you spun up application files under `runtime/` for verification,
note that `runtime/` is gitignored, so nothing leaks into the repo.
The skill itself produces no scratch state.
