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

## Updating the running UI (canonical flow)

The deployed system interface is the live web UI the user is looking at, so
changes are not applied in place. The canonical flow is the
`update-system-interface` agent skill: a change is delegated to a worker, tested
in isolation (including Playwright against an isolated instance) and run through
the review gates; then **previewed** to the user as a tab before merging; and,
once approved, merged and revealed. See
`.agents/skills/update-system-interface/SKILL.md`.

The same `reveal_system_interface.py` script owns the deterministic setup/teardown
on both sides of that user gate, as sub-commands:

- `preview --slug <name> --work-dir <worker-work-dir>` boots the worker's
  already-built work_dir (a local worktree-agent folder in this same container)
  on a free port and registers it as the `si-preview-app` service, then boots a
  small wrapper page that embeds it in a labeled "preview" frame and registers
  that as the user-facing `si-preview` service -- so the proxied tab reads as a
  clearly-marked proposed change rather than a nested clone of the live UI. No
  fetch, no re-checkout, no rebuild, and without merging or touching the served
  tree. (Resolve the work_dir from
  `mngr ls --include 'name=="<name>"' --format json` -> `agents[0].work_dir`.)
- `unpreview --slug <name>` tears that down -- kill both servers, deregister both
  services (idempotent).
- `reveal --rollback-to <sha>` reveals the merged change (below).

The reveal, after merge, is a single self-healing command. With the known-good
revision captured before the merge (`ROLLBACK_TO=$(git rev-parse HEAD)`):

```bash
python3 .agents/skills/update-system-interface/scripts/reveal_system_interface.py reveal --rollback-to "$ROLLBACK_TO"
```

It classifies what changed and does only what is needed: refreshes dependencies
if a manifest changed (`npm ci` / `uv tool install -e apps/system_interface
--reinstall`), rebuilds the gitignored `static/` bundle and broadcasts a
`reload_system_interface` op (frontend), and/or restarts the services agent so
the editable backend re-imports the merged `.py` (backend). For a backend change
it pre-flights the merged code on a throwaway port before touching the live
service, then polls the loopback endpoint to confirm health. If anything fails,
it restores the tree to `--rollback-to` as a forward revert commit, rebuilds and
restarts from it, and re-confirms the UI is healthy -- so the served interface
can never be left broken. The exit code reports the outcome (`0` revealed, `2`
rolled back, `3` emergency, `1` precondition error).

The `reload_system_interface` op it broadcasts goes to the loopback-only
`/api/layout/broadcast` endpoint, which relays a `layout_op` WebSocket message;
the dockview shell (`DockviewWorkspace.ts`) reloads the top-level page -- shell
chrome plus every child chat iframe -- so the browser picks up the new hashed
assets. This is distinct from `scripts/layout.py refresh`, which only reloads a
single inner iframe/panel for arranging the workspace.

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
# (reports a no-op if one is already open; use ``focus`` to bring it
# to the foreground).
python3 scripts/layout.py open web

# Reload one tab (or, for ``service:<name>``, every iframe tied to
# that service).
python3 scripts/layout.py refresh web

# Inspect the live grid tree -- arrangements, sizes, active panel,
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
