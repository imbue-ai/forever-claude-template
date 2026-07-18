# The global (public) URL

A scaffolded service is always reachable *inside* the workspace at
`http://127.0.0.1:8000/service/<name>/` and through the desktop client's tab.
It is **not** automatically reachable from outside the workspace -- even when
the `cloudflared` tunnel service is running.

## A service is not public until the user shares it

A service only gets a public URL once the user explicitly **shares** it: in the
Minds desktop client, open **workspace settings**, find the app, and use its
**Share** action. That action is what publishes the app and defines its public
URL. Until then there is no public URL, and a running `cloudflared` tunnel by
itself does not create one.

This step is easy to miss and easy to describe unclearly -- do not assume a
service is publicly reachable just because the tunnel is up, and do not tell the
user a public URL already exists before they have shared the app. If the user
wants to open a service on another device (e.g. their phone), the first thing to
tell them is to share it from workspace settings.

## Where the public hostname comes from

The public hostname is assigned **server-side** by the Minds platform and is not
derivable from inside the workspace:

- The tunnel runner (`libs/cloudflare_tunnel/runner.py`) only runs
  `cloudflared tunnel run --token <token>`. The token encodes the Cloudflare
  account and tunnel id, not a hostname; the hostname -> `localhost:8000`
  ingress mapping lives in Cloudflare's configuration on the platform side.
- Nothing in the workspace container (`apps/system_interface`,
  `libs/cloudflare_tunnel`, `vendor/mngr`) constructs or stores the public host.
- It is **not** written into `runtime/applications.toml` -- `forward_port.py`
  stores only `name` and the local `http://localhost:<port>` backend URL. Do not
  grep that file for a public URL, and do not skim `cloudflared` logs for one.

Once shared, the service is reachable at:

```
https://<workspace-public-host>/service/<name>/
```

## Getting the exact URL

The desktop client resolves the public host via the platform's services API to
render tabs, so the reliable ways to obtain the full URL are:

- the **Share** action above (it surfaces the URL when it publishes the app), or
- asking the user to read/copy it from the shared-app entry.

Note the desktop client is a native app with **no browser address bar** -- do
not instruct the user to read the URL from one.

If the workspace has no tunnel token configured, none of this applies -- the
local `http://127.0.0.1:8000/service/<name>/` URL is the only entry point.
