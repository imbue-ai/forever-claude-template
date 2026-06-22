# Web service gotchas

The system_interface proxies HTTP and WebSocket traffic from
`/service/<name>/...` to the backend URL you registered. Most apps
"just work" -- the Flask scaffolder picks defaults that sidestep
the common traps. This file is loaded on demand when verification
surfaces something odd; skim for the symptom that matches.

## "I see the chat tab again with a duplicated dockview tab bar"

Symptom: the user clicks the service tab, and instead of the app,
they see the agent's chat interface again, sometimes with a duplicate
tab bar at the top.

Root cause: the system_interface could not reach the registered
backend, so the request fell through to the top-level UI. Either:

- The backend never came up (check `supervisorctl status <name>` and
  `/var/log/supervisor/<name>-stderr.log`).
- The backend bound to a different host than what was registered
  (e.g. bound to a Unix socket, or to an interface the system_interface
  cannot reach inside the container).
- The `--name` passed to `forward_port.py` does not match the URL
  segment the user clicked.

Fix: re-check pre-flight (bind to 127.0.0.1, port matches supervisord.conf,
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

## Prefix-aware links (Flask serves at `/`)

The Flask starter serves at `/` and needs no prefix configuration.
The system_interface proxy handles the `/service/<name>/` prefix for
you: it rewrites absolute paths in the served HTML, and installs a
scoped service worker that prepends the prefix to the page's own
fetches. So a Flask app written against `/` works behind the proxy
unchanged.

The one thing to watch: if you generate prefix-aware links yourself
in application code (e.g. building an absolute URL to share or embed),
account for the `/service/<name>/` prefix in that code. There is no
`root_path`/`ROOT_PATH` mechanism in the generated runner -- don't
add one.

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
(`<name>-admin`, `<name>-metrics`). `forward_port.py` only registers
one URL per service name.

## Port already in use

If the port you chose is bound by something else, the start command
will fail loudly (the framework will print an error and exit). With
`autorestart=true`, supervisord will keep restarting it, producing a
crash loop visible via `supervisorctl status <name>` and
`/var/log/supervisor/<name>-stderr.log`. Pick a different port.

The scaffolder's port-picking pre-flight (which parses `supervisord.conf`
and `runtime/applications.toml`) catches this before you write the
program entry. For the wrap-existing escape hatch, run `ss -tln`
manually before choosing a port.

## Bind host (wrap-existing path mostly)

The scaffolder generates
`run_simple("127.0.0.1", port, app, threaded=True, ...)` (werkzeug)
which is correct. For the wrap-existing escape hatch, many Node
frameworks default to `0.0.0.0` (Node's
`http.createServer().listen(port)` binds to `::`/`0.0.0.0` when no
host is passed). Pass an explicit loopback host
(`HOST=127.0.0.1`, `app.listen(port, "127.0.0.1")`, etc.) to keep the
proxy working consistently and to avoid noise.
