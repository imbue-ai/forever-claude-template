# Minds Workspace Server

Web chat interface for viewing and interacting with mngr-managed Claude agents.

Shows live conversations from Claude session files in a web UI, with real-time
updates via Server-Sent Events.

## Usage

```bash
minds-workspace-server
```

Opens at http://127.0.0.1:8000 by default.

## Development

```bash
# Backend
cd apps/system_interface
uv run minds-workspace-server

# Frontend (with hot reload)
cd apps/system_interface/frontend
npm install
npm run dev
```

## Surfacing and refreshing web-service tabs from an agent

An agent running inside the workspace container can tell the workspace UI
to either open a tab for one of its services or reload any already-open
tab. Both flows go through the agent-facing `scripts/web_view.py` helper:

```bash
# Print every user-facing registered service name (one per line; the
# workspace chrome's own `system_interface` entry is hidden).
python3 scripts/web_view.py list

# Open the given service in a tab split alongside the primary chat
# (focuses an existing tab if one is already open).
python3 scripts/web_view.py open web

# Reload every open iframe tab for the given service.
python3 scripts/web_view.py refresh web
```

The script POSTs to loopback-only endpoints on the workspace server
(`/api/open-tab/<name>/broadcast` and `/api/refresh-service/<name>/broadcast`)
which emit `open_tab` / `refresh_service` messages over the workspace-server
WebSocket. The frontend matches iframe tabs by their `data-service-name`
attribute. Replace `web` with whichever service name (as listed in
`runtime/applications.toml` / the tab dropdown) you want to act on.

## Building

```bash
cd apps/system_interface/frontend
npm run build
```

This compiles the frontend into `imbue/minds_workspace_server/static/`.
