---
name: manage-layout
description: Use when you want to rearrange the workspace dockview tabs (split, move, focus, rename, close, maximize, reload, swap a URL) or inspect the live layout. The mechanics are in scripts/layout.py -- this skill just orients you.
metadata:
  crystallized: true
---

# Managing the workspace dockview layout

The dockview tab strip in the desktop client is fully scriptable from
your agent via `scripts/layout.py`. Use it whenever you want to:

- **Surface something new** alongside the chat (web view, terminal,
  another agent's chat).
- **Inspect** what's currently open, so you can reason about where to
  put the new panel.
- **Rearrange** what's already open -- split, move, focus, rename,
  maximize/restore, replace an iframe's URL, or close a tab.
- **Refresh** an iframe after redeploying its backing service.

The helper is preferred over driving the dockview through ad-hoc API
calls: it serializes mutating ops through an advisory mutex, dispatches
through a single loopback endpoint, and uses stable type-prefixed refs
that don't renumber as panels open and close.

## Refs: how you address a panel

Every panel has a stable, type-prefixed ref:

| Prefix | Meaning | Example |
|---|---|---|
| `service:<name>` | The iframe for a registered workspace service. | `service:web` |
| `chat:<agent-name>` | The chat tab for an mngr-level agent. | `chat:alice` |
| `subagent:<session-id>` | A harness-level subagent panel. | `subagent:abcd1234` |
| `terminal:<short-hash>` | An ad-hoc terminal tab. | `terminal:1a2b3c4d` |
| `url:<short-hash>` | An ad-hoc external URL tab. | `url:9f8e7d6c` |

Subcommands that take a "service or ref" argument (`open`, `split`,
`refresh`) also accept a bare service name (`web`) -- it expands to
`service:web`. The literal `self` (only valid as `--relative-to`)
resolves to the caller's own chat panel.

Run `uv run python scripts/layout.py inspect` to see refs for the
currently-open panels.

## Common operations

| Goal | Command |
|---|---|
| List addressable things (services + agents) with open/running flags | `python3 scripts/layout.py list` |
| Inspect the live tree (orientation, sizes, active panel) | `python3 scripts/layout.py inspect` |
| Surface a service alongside the primary chat | `python3 scripts/layout.py open web` |
| Reload one tab after redeploying | `python3 scripts/layout.py refresh web` |
| Add a second panel below an existing one | `python3 scripts/layout.py split api --relative-to=service:web --direction=below --ratio=0.4` |
| Focus an existing tab | `python3 scripts/layout.py focus service:web` |
| Move a tab next to another | `python3 scripts/layout.py move chat:alice --relative-to=service:web --direction=right` |
| Rename a tab's label | `python3 scripts/layout.py rename service:web "Customer dashboard"` |
| Maximize / restore a group | `python3 scripts/layout.py maximize service:web` / `python3 scripts/layout.py restore` |
| Point an iframe at a new URL | `python3 scripts/layout.py replace-url service:web service:web/admin` |
| Close a tab | `python3 scripts/layout.py close url:9f8e7d6c` |

Output for `list` and `inspect` is YAML by default; pass `--json` if
you want to consume it programmatically.

For anything you don't see above, run
`python3 scripts/layout.py --help` and the per-subcommand `--help`.

## Exit codes

`layout.py` uses distinct exit codes so wrapper scripts can branch:

- `0` ok
- `2` requested service not registered yet (and registration polling
  timed out -- did `forward_port.py` run?)
- `3` couldn't reach the workspace server
- `4` HTTP error other than the codes below
- `5` mutex conflict -- another agent's layout op is in flight. The
  stderr message includes the in-flight op's `agent_id`, `op`, `args`,
  `started_at`, and `retry_after_ms`. Decide whether to retry.
- `6` not-found (e.g. the ref you named doesn't match an open panel)
- `7` bad request (malformed ref, unknown direction, unsupported URL)

## When NOT to use this skill

- **Building a brand-new web service.** Use `build-web-service` to
  scaffold the service first; `build-web-service` itself ends with a
  `layout.py open <name>` call to surface the new tab.
- **Persisting layout state.** The frontend auto-saves the layout on
  every change; you don't need to do anything special after a mutation.
