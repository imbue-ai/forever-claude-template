# System Interface

Web chat interface for viewing and interacting with mngr-managed Claude agents.

Shows live conversations from Claude session files in a web UI, with real-time
updates via Server-Sent Events.

## Usage

```bash
system-interface
```

Opens at http://127.0.0.1:8000 by default.

## Development

```bash
# Backend
cd apps/system_interface
uv run system-interface

# Frontend (with hot reload)
cd apps/system_interface/frontend
npm install
npm run dev
```

## Refreshing web-service tabs from an agent

An agent running inside the workspace container can tell the user's Minds UI
to reload any open tab for one of its web services. The agent POSTs to the
system interface on localhost (default port 8000, matching
`Config.system_interface_port`):

```bash
curl -X POST "http://127.0.0.1:8000/api/refresh-service/web"
```

This appends a `refresh_service` event to the agent's
`events/refresh/events.jsonl` file. The minds desktop client tails the event
via `mngr event --follow`, then POSTs back to the system interface which
broadcasts a WebSocket message telling the frontend to reload every open
iframe tab tied to the given service (matched by the iframe's
`data-service-name` attribute). Replace `web` with whichever service name
(as listed in `runtime/applications.toml` / the tab dropdown) you want to
refresh.

## Building

```bash
cd apps/system_interface/frontend
npm run build
```

This compiles the frontend into `imbue/system_interface/static/`.
