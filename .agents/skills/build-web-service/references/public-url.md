# The global (Cloudflare) URL

If the workspace has Cloudflare tunneling configured, the service is
also reachable at a public URL in addition to the local one. Two
caveats:

- **The public hostname is owned server-side**, not by the
  cloudflared process running in this container. Skimming the
  `cloudflared` service's logs will not surface a URL.
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
