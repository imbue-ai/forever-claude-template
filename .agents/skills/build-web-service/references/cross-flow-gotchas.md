# Web service gotchas

The system_interface proxies HTTP and WebSocket traffic from
`/service/<name>/...` to the backend URL you registered. Most apps
"just work" -- the FastAPI scaffolder picks defaults that sidestep
the common traps. This file is loaded on demand when verification
surfaces something odd; skim for the symptom that matches.

## "I see the chat tab again with a duplicated dockview tab bar"

Symptom: the user clicks the service tab, and instead of the app,
they see the agent's chat interface again, sometimes with a duplicate
tab bar at the top.

Root cause: the system_interface could not reach the registered
backend, so the request fell through to the top-level UI. Either:

- The backend never came up (check `tmux capture-pane -t svc-<name> -p`).
- The backend bound to a different host than what was registered
  (e.g. bound to a Unix socket, or to an interface the system_interface
  cannot reach inside the container).
- The `--name` passed to `forward_port.py` does not match the URL
  segment the user clicked.

Fix: re-check pre-flight (bind to 127.0.0.1, port matches services.toml,
name matches the URL segment) and Step 3 verification.

## Backend redirects (3xx Location headers)

Backends often return absolute paths in `Location` (e.g.
`Location: /login`). Without rewriting, the browser would navigate to
`https://workspace-host/login` -- which is the system_interface's
top-level path, not your service.

The system_interface rewrites `Location` headers on 3xx responses:

- Absolute paths get prefixed: `/login` -> `/service/<name>/login`.
- Same-origin absolute URLs targeting the backend's own host:port get
  rewritten to proxy-relative paths under the service prefix.
- External URLs (`https://example.com/`) and protocol-relative URLs
  pass through unchanged.

What this means for you: if your app emits relative `Location`s or
absolute paths under its own root, redirects work. If it hardcodes
public URLs at non-prefixed paths, those will land at the wrong place.

## Client-side URLs: emit relative paths, never the prefix

This is the single most common prefix bug. Your app is at
`/service/<name>/` behind the proxy and at `/` standalone, so **every
URL your HTML/JS builds -- `fetch`, iframe `src`, form `action`,
`<a href>`, WebSocket URLs -- must be RELATIVE** (`raw/123`,
`api/items`), never absolute and never a hardcoded prefix.

The proxy injects `<base href="/service/<name>/">`, so a relative URL
resolves under the prefix behind the proxy and under `/` standalone.

- **Absolute path** (`/raw/123`): ignores `<base>`, resolves against the
  origin root, escapes your service, and hits the workspace shell. The
  classic symptom is an iframe whose `src` is `/raw/123` rendering blank
  -- it loaded the workspace UI (and any script there is killed by the
  iframe sandbox), not your route.
- **Hardcoded prefix** (`/service/<name>/raw/123`): works behind the
  proxy, breaks standalone, rots on rename.

Do not read the prefix at runtime to prepend it yourself -- `ROOT_PATH`
is a server-only env var, not reliably present in client code. Just emit
the relative path. (The WebSocket section below is one instance of this
same rule.)

## FastAPI absolute URLs (OpenAPI, redirects) -- the server-side half

FastAPI emits absolute URLs in OpenAPI metadata (`/docs`,
`/openapi.json`) and from `RedirectResponse`/`request.url_for` based on
`app.root_path`. The scaffolder reads `ROOT_PATH` from env, passes it to
`FastAPI(root_path=ROOT_PATH)`, and the generated services.toml command
sets `ROOT_PATH=/service/<name>` **on the app process**
(`... && ROOT_PATH=/service/<name> uv run <name>`).

Watch the placement: a `VAR=val cmd1 && cmd2` prefix binds `VAR` to
`cmd1` only. If `ROOT_PATH=` sits at the front of the whole command it
reaches `forward_port.py` (which ignores it) and the app's env stays
empty -- so `root_path` is `""` and every server-generated URL is
mis-prefixed, even though the line *looks* correct. The scaffolder puts
it in the right place; if you hand-edit the command or use the
wrap-existing escape hatch, keep the assignment on the app.

`root_path` only fixes URLs FastAPI generates **server-side**. It does
nothing for URLs in the markup you emit -- those follow the relative-URL
rule above. A correct setup uses both halves.

If you wrote your own FastAPI runner without using the scaffolder, set
`root_path=/service/<name>` at construction time or via the same
`ROOT_PATH` env-var pattern (on the app process). Without it,
`/openapi.json` will list endpoints at `/`, breaking the API explorer.

## Static-file servers and trailing slashes

`python -m http.server` and similar serve `index.html` at
`/some-dir/` but redirect `/some-dir` (no trailing slash) to
`/some-dir/`. Combined with a service prefix, the redirect target
needs the prefix added. The system_interface's Location-rewriting
handles this; just make sure you hit `/service/<name>/` (with the
trailing slash) the first time. The desktop client tab does this by
default.

## WebSockets

The system_interface proxies WebSocket upgrades under
`/service/<name>/<ws-path>`. This is an instance of the relative-URL
rule above: pass a **relative** path to the `WebSocket` constructor.
Modern browsers resolve it against the document base URL -- which
includes the proxy's injected `<base href="/service/<name>/">` -- and
the URL parser upgrades the scheme automatically (`http:` -> `ws:`,
`https:` -> `wss:`), so the same code is correct behind the proxy and
standalone, and HTTPS-served pages (e.g. via the Cloudflare tunnel)
get `wss:` without a mixed-content error:

```js
new WebSocket("socket");
```

Older browsers (predating relative-URL support in the `WebSocket`
constructor) need an absolute URL. Build it from `location` so you
still avoid a hardcoded host, and derive the scheme from
`location.protocol` so HTTPS pages use `wss:`:

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
(`<name>-admin`, `<name>-metrics`). `forward_port.py` only registers
one URL per service name.

## Port already in use

If the port you chose is bound by something else, the start command
will fail loudly inside `svc-<name>` (the framework will print an
error and exit). The bootstrap manager will keep restarting it if
`restart = "on-failure"`, producing a tight crash loop visible in
`tmux capture-pane -t svc-<name> -p`. Pick a different port.

The scaffolder's port-picking pre-flight (which parses `services.toml`
and `runtime/applications.toml`) catches this before you write the
service entry. For the wrap-existing escape hatch, run `ss -tln`
manually before choosing a port.

## Bind host (wrap-existing path mostly)

The scaffolder generates `uvicorn.run(app, host="127.0.0.1", port=...)`
which is correct. For the wrap-existing escape hatch, many Node
frameworks default to `0.0.0.0` (Node's
`http.createServer().listen(port)` binds to `::`/`0.0.0.0` when no
host is passed). Pass an explicit loopback host
(`HOST=127.0.0.1`, `app.listen(port, "127.0.0.1")`, etc.) to keep the
proxy working consistently and to avoid noise.
