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
`update-system-interface` agent skill -- the system-interface specialization of
`update-service`'s "live loop first, ratify at turn-end" pattern. The lead edits
an **isolated worktree**, builds, and refreshes a labeled **preview tab** in
place, iterating live with the user (seconds per round, not a full harden pass);
once the user approves the shape, a background worker runs the full test + review
gate on that same branch; then the change is merged and **revealed** to the live
UI with auto-rollback. See `.agents/skills/update-system-interface/SKILL.md`.

The `reveal_system_interface.py` script owns the deterministic parts of that flow
as sub-commands:

- `preview --slug <name> --work-dir <work-dir>` boots an already-built work_dir
  (the lead's editing worktree during the live loop, or a worker's work_dir for a
  final pre-merge preview) on a free port and registers it as the `si-preview-app`
  service, then boots a small wrapper page that embeds it in a labeled "preview"
  frame and registers that as the user-facing `si-preview` service -- so the
  proxied tab reads as a clearly-marked proposed change rather than a nested clone
  of the live UI. No fetch, no re-checkout, no rebuild, and without merging or
  touching the served tree. (For a worker's work_dir, resolve it from
  `mngr ls --include 'name=="<name>"' --format json` -> `agents[0].work_dir`.)
- `preview-refresh --slug <name>` re-boots the preview's inner app on its existing
  port to pick up a backend edit/rebuild during the live loop, without disturbing
  the wrapper frame or the user's tab (a frontend-only round needs no bounce --
  just rebuild and `layout.py refresh si-preview`).
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

## Named layouts

The dockview state is persisted as *named layouts* -- one JSON file per
layout under the primary agent's `workspace_layout/layouts/` directory,
with a `layouts_meta.json` registry (display names + last-active slug).
Two defaults, `desktop` and `mobile`, always exist as names; a layout
with no saved content renders as the fresh welcome-chat state. A
pre-existing single `layout.json` is migrated into `desktop` on first
access.

Each browser client picks its layout on first connect by user agent
(mobile browsers get `mobile`, everything else `desktop`), remembers
the choice in localStorage, and can switch via the "+" menu's
"Save layout... / Load layout... / Delete layout..." dialogs. Autosaves
target the client's active layout; when one client saves a layout,
other clients with it active re-apply it live. The REST surface is
`GET /api/layouts`, `GET|POST /api/layouts/<slug>`,
`POST /api/layouts` (save-as, server-side slugification), and
`POST /api/layouts/<slug>/delete` (the last layout cannot be deleted).

Chat messages sent through the UI (and every layout switch) are logged
to `workspace_layout/events/client_activity/events.jsonl` with the
sending client's id, device kind, and active layout, so agents can
attribute a request to a client via `layout.py context`.

## Driving the workspace layout from an agent

An agent running inside the workspace container can rearrange the
dockview through the agent-facing `scripts/layout.py` helper. The
subcommand surface covers `list / inspect / where / context / load /
open / focus / split / close / move / rename / maximize / restore /
replace-url / refresh`.

```bash
# Print every addressable thing (registered services + mngr agents)
# with open/running flags. YAML by default, ``--json`` to switch.
python3 scripts/layout.py list

# See which browser clients exist, their device kind, current layout,
# and recent messages (to attribute a request to a client/layout).
python3 scripts/layout.py context

# Surface the given service in a tab split alongside the primary chat
# (reports a no-op if one is already open; use ``focus`` to bring it
# to the foreground). Mutating ops always name their target layout.
python3 scripts/layout.py open web --layout desktop

# Reload one tab (or, for ``service:<name>``, every iframe tied to
# that service).
python3 scripts/layout.py refresh web

# Inspect the grid tree -- arrangements, sizes, active panel,
# ref-resolved panel list -- of the last-active (or named) layout.
python3 scripts/layout.py inspect --layout mobile
```

Every op POSTs `{op, args, agent_id}` to the loopback-only
`/api/layout/broadcast` endpoint on the system interface. Mutating ops
require a target layout, are delivered only to connected clients with
that layout active (HTTP 412 when there are none -- `load` a layout
onto a client first), and acquire an in-process advisory mutex (HTTP
409 with the in-flight holder's metadata on contention); reads bypass
both. Panels are addressed by stable, type-prefixed refs:
`service:<name>`, `chat:<agent-name>`, `subagent:<session-id>`,
`terminal:<short-hash>`, `url:<short-hash>`. Subcommands that take a
"service or ref" argument also accept a bare service name (e.g. `web`
-> `service:web`). See the `manage-layout` skill for end-to-end
orientation.

## Building

```bash
cd apps/system_interface/frontend
npm run build
```

This compiles the frontend into `imbue/system_interface/static/`.
