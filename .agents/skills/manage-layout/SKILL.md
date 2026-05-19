---
name: manage-layout
description: Use when you want to rearrange the workspace dockview tabs (split, move, focus, rename, close, maximize, reload, swap a URL) or inspect the live layout.
metadata:
  crystallized: true
---

# Managing the workspace dockview layout

The user interacts with you and the services you create through a tabbed dockview interface defined in `apps/system_interface`.
The user's chat with you is visible as one such tab in this interface (of ref-form `chat:name` where `name` is your name).
They may additionally have a terminal view open of your chat interface; but the direct chat view is their primary interaction point with you.
This client is fully scriptable from via `scripts/layout.py`. Use it whenever you want to:

- **Surface something new** alongside the chat (web view, terminal,
  another agent's chat).
- **Inspect** what's currently open, so you can reason about where to
  put the new panel.
- **Rearrange** what's already open -- split, move, focus, rename,
  maximize/restore, replace an iframe's URL, or close a tab.
- **Refresh** an iframe after redeploying its backing service.

The user may also ask you to do any of these things, in which case you should use the appropriate commands.
You should only use this helper to mutate the view; do not manually edit the dockview layout configuration.

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
`service:web`. The literal `self` resolves to your own chat
panel; it is accepted as a ref anywhere (most usefully as
`--relative-to=self` for `split` / `move`).

Run `python3 scripts/layout.py inspect` to see refs for the
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

## Share-existing-group vs. new-group

`open`, `split`, and `move` default to **tabbing into an existing group**
that already lives in the requested direction relative to the anchor.
For example, `open service:terminal` from your chat with a `service:web`
group already to the right adds `terminal` as a tab inside the web
group rather than wedging another column between the two. Pass
`--new-group` when you genuinely want a fresh column / row instead:

```
python3 scripts/layout.py split api --relative-to=service:web --direction=below --new-group
```

## Exit codes

`layout.py` uses distinct exit codes so wrapper scripts can branch:

- `0` ok
- `2` argparse CLI usage error (unknown subcommand, invalid `--direction`
  choice, missing required argument). Not emitted by `layout.py` itself.
- `10` requested service not registered yet (and registration polling
  timed out -- did `forward_port.py` run?)
- `11` couldn't reach the workspace server
- `12` HTTP error other than the codes below
- `13` mutex conflict -- another agent's layout op is in flight. The
  stderr message includes the in-flight op's `agent_id`, `op`, `args`,
  `started_at`, and `retry_after_ms`. Decide whether to retry.
- `14` not-found (e.g. the ref you named doesn't match an open panel)
- `15` bad request (malformed ref, unknown direction, unsupported URL)

## When NOT to use this skill

- **Building a brand-new web service.** Use `build-web-service` to
  scaffold the service first; `build-web-service` itself ends with a
  `layout.py open <name>` call to surface the new tab.
- **Persisting layout state.** The frontend auto-saves the layout on
  every change; you don't need to do anything special after a mutation.
