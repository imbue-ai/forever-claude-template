# Framework gotchas for `/service/<name>/` exposure

The workspace_server proxies HTTP and WebSocket traffic from
`/service/<name>/...` to the backend URL you registered. Most apps
"just work", but a few framework behaviors interact badly with the
prefix and produce confusing failures. This file is loaded on demand
when verification surfaces something odd -- skim it for the symptom
that matches.

## "I see the chat tab again with a duplicated dockview tab bar"

The motivating bug for this skill. Symptom: the user clicks the
service tab, and instead of the app, they see the agent's chat
interface again, sometimes with a duplicate tab bar at the top.

Root cause: the workspace_server could not reach the registered
backend, so the request fell through to the top-level UI. Either:

- The backend never came up (check `tmux capture-pane -t svc-<name> -p`).
- The backend bound to a different host than what was registered (e.g.
  bound to a Unix socket, or to an interface the workspace_server
  cannot reach inside the container).
- The `--name` passed to `forward_port.py` does not match the URL
  segment the user clicked.

Fix: re-check Steps 1, 2, 5 of the main SKILL.md.

## Backend redirects (3xx Location headers)

Backends often return absolute paths in `Location` (e.g.
`Location: /login`). Without rewriting, the browser would navigate to
`https://workspace-host/login` -- which is the workspace_server's
top-level path, not your service.

The workspace_server rewrites `Location` headers on 3xx responses:

- Absolute paths get prefixed: `/login` -> `/service/<name>/login`.
- Same-origin absolute URLs targeting the backend's own host:port get
  rewritten to proxy-relative paths under the service prefix.
- External URLs (`https://example.com/`) and protocol-relative URLs
  pass through unchanged.

What this means for you: if your app emits relative `Location`s or
absolute paths under its own root, redirects work. If it hardcodes
public URLs at non-prefixed paths, those will land at the wrong place.

## uvicorn / FastAPI mount paths

FastAPI emits absolute URLs in OpenAPI metadata (`/docs`, `/openapi.json`)
based on `app.root_path`. Because the workspace_server strips the
prefix before forwarding, your app sees requests at `/`, not
`/service/<name>/`. Two options:

- **Do nothing if you do not generate absolute URLs.** Most apps work
  fine; routes resolve relatively in the browser.
- **Set `root_path` if you do.** When running under uvicorn, pass
  `--root-path /service/<name>` so FastAPI knows the public prefix
  and emits OpenAPI links correctly.

## Static-file servers and trailing slashes

`python -m http.server` and similar serve `index.html` at
`/some-dir/` but redirect `/some-dir` (no trailing slash) to
`/some-dir/`. Combined with a service prefix, the redirect target
needs the prefix added. The workspace_server's Location-rewriting
handles this; just make sure you hit `/service/<name>/` (with the
trailing slash) the first time. The desktop client tab does this by
default.

## WebSockets

The workspace_server proxies WebSocket upgrades under
`/service/<name>/<ws-path>`. Your client code should connect to a
relative URL and derive the scheme from `location.protocol` so that
HTTPS-served pages (e.g. via the Cloudflare tunnel) use `wss:` --
hardcoding `ws:` will be blocked by browsers as mixed content on
HTTPS:

```js
const scheme = location.protocol === "https:" ? "wss:" : "ws:";
new WebSocket(scheme + "//" + location.host + "/service/<name>/socket");
```

Do not hardcode `ws://localhost:<port>` either. Same constraint as
HTTP: relative paths "just work"; hardcoded absolute backend URLs do
not.

## Multiple ports per app

If your app listens on more than one port (rare, but happens with
admin UIs or metrics endpoints), expose each as its own service
(`<name>-admin`, `<name>-metrics`). The forwarder only registers one
URL per service name.

## Port already in use

If the port you chose is bound by something else, the start command
will fail loudly inside `svc-<name>` (the framework will print an
error and exit). The bootstrap manager will keep restarting it if
`restart = "on-failure"`, producing a tight crash loop visible in
`tmux capture-pane -t svc-<name> -p`. Pick a different port.

The skill's pre-flight (`ss -tln`) catches this before you write the
service entry.
