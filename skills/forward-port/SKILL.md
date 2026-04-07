---
name: forward-port
description: Create a service that exposes an application port via both local and global (Cloudflare) forwarding
---

# How to forward an application port

Applications are services that expose HTTP (or other) ports that should be accessible
to the user. Each application gets two URLs:
1. A local forwarding URL via the minds forwarding server
2. A global Cloudflare tunnel URL (if global forwarding is enabled)

**Important: Always create a proper service rather than forwarding a port directly.**
Ports forwarded without a service definition in services.toml will not survive container restarts.

## Step 1: Create a wrapper script (if needed)

If the application needs setup before starting, create a script in `scripts/`:

```bash
#!/usr/bin/env bash
set -euo pipefail
# Register the port before starting the server
python3 scripts/forward_port.py --url http://localhost:PORT --name my-app
exec my-server-command --port PORT
```

For simple cases, you can inline it directly in services.toml (see step 2).

## Step 2: Add a service to services.toml

Edit `services.toml` to add your service. Use the `edit-services` skill for guidance.

For a simple inline command:
```toml
[services.my-app]
command = "python3 scripts/forward_port.py --url http://localhost:3000 --name my-app && exec node server.js"
```

For a wrapper script:
```toml
[services.my-app]
command = "bash scripts/run_my_app.sh"
```

The bootstrap service manager will automatically detect the change and start the service.

## Step 3: Verify

After the service starts:
- The `app-watcher` service will detect the new entry in `runtime/applications.toml`
- It will register the application with Cloudflare (if a tunnel token is configured)
- It will write server events so the forwarding server discovers the new backend
- Both local and global URLs will become available

## forward_port.py reference

```
python3 scripts/forward_port.py --url URL --name NAME [--no-global]
python3 scripts/forward_port.py --remove --name NAME
```

Arguments:
- `--url`: Full URL where the application is accessible (e.g., `http://localhost:8080`)
- `--name`: Application name (becomes part of the Cloudflare URL)
- `--no-global`: Disable global Cloudflare forwarding for this application
- `--remove`: Remove the application entry from applications.toml

## Notes

- Application names should be short, lowercase, alphanumeric with hyphens
- The `global` flag defaults to `true` -- most applications should be globally accessible
- The app-watcher handles all Cloudflare API calls automatically
- If you need to disable global forwarding temporarily, use `--no-global`
