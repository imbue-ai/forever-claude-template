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

## Driving the workspace layout from an agent

An agent running inside the workspace container can rearrange the
dockview through the agent-facing `scripts/layout.py` helper. The
subcommand surface covers `list / inspect / open / focus / split /
close / move / rename / maximize / restore / replace-url / refresh`.

```bash
# Print every addressable thing (registered services + mngr agents)
# with open/running flags. YAML by default, ``--json`` to switch.
python3 scripts/layout.py list

# Surface the given service in a tab split alongside the primary chat
# (focuses an existing tab if one is already open).
python3 scripts/layout.py open web

# Reload one tab (or, for ``service:<name>``, every iframe tied to
# that service).
python3 scripts/layout.py refresh web

# Inspect the live grid tree -- orientations, sizes, active panel,
# ref-resolved panel list.
python3 scripts/layout.py inspect
```

Every op POSTs `{op, args, agent_id}` to the loopback-only
`/api/layout/broadcast` endpoint on the system interface. Mutating ops
acquire an in-process advisory mutex (HTTP 409 with the in-flight
holder's metadata on contention); reads bypass it. Panels are
addressed by stable, type-prefixed refs: `service:<name>`,
`chat:<agent-name>`, `subagent:<session-id>`, `terminal:<short-hash>`,
`url:<short-hash>`. Subcommands that take a "service or ref" argument
also accept a bare service name (e.g. `web` -> `service:web`). See the
`manage-layout` skill for end-to-end orientation.

## Building

```bash
cd apps/system_interface/frontend
npm run build
```

This compiles the frontend into `imbue/system_interface/static/`.
