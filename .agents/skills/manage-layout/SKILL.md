---
name: manage-layout
description: Use when you want to rearrange the workspace dockview tabs (split, move, focus, rename, close, maximize, reload, swap a URL) or inspect the live layout.
metadata:
  crystallized: true
---

# Managing the workspace dockview layout

The user interacts with you (and the services you create) through a
tabbed dockview defined in `apps/system_interface`. Your chat is one
such tab; everything else -- service iframes, terminals, ad-hoc URL
tabs, other agents' chats -- lives alongside it.

`scripts/layout.py` is the agent-facing helper. Use it whenever you
want to surface, inspect, or rearrange tabs. Do not hand-edit the
dockview layout config.

## The two verbs you'll use 95% of the time

| Goal | Command |
|---|---|
| See what's currently open and how it's laid out | `python3 scripts/layout.py inspect` |
| List everything addressable (services + agents) with open/running flags | `python3 scripts/layout.py list` |
| Surface a service / URL / terminal / chat alongside your chat | `python3 scripts/layout.py open <target>` |
| Close a tab | `python3 scripts/layout.py close <ref>` |

`open` is the opinionated default. It puts the new tab to the right
of your chat, joining whatever group already lives there if one is
open. Targets it accepts:

- A workspace service name (`web`) -- focuses an existing iframe for
  that service if one is open; otherwise creates one.
- `terminal` -- creates a fresh terminal in the primary agent's
  work_dir (each call adds a new one, just like the UI's "New
  terminal" button). The new tab's ref (`terminal:<hash>`) is printed
  to stdout so you can capture it for later ops.
- An external URL (`https://example.com`) -- focuses an existing
  ad-hoc URL tab pointed at that URL, otherwise opens one.
- A chat ref (`chat:alice`) -- opens another mngr-level agent's chat.

Pass `--new-group` if you specifically want a fresh column instead of
joining an existing right-side group. Reach for it when you want the
new tab to be visually adjacent and full-height rather than mixed in
with whatever else is open to the right.

## Refs: how every panel is addressed

Every panel has a stable, type-prefixed ref returned by `inspect`:

| Prefix | Meaning | Example |
|---|---|---|
| `service:<name>` | The iframe for a registered workspace service. | `service:web` |
| `chat:<agent-name>` | The chat tab for an mngr-level agent. | `chat:alice` |
| `terminal:<short-hash>` | A terminal tab (one ref per terminal, since each is independent). | `terminal:1a2b3c4d` |
| `url:<short-hash>` | An ad-hoc external URL tab. | `url:9f8e7d6c` |
| `subagent:<session-id>` | A harness-level subagent panel. | `subagent:abcd1234` |

When you call `open terminal`, the new tab's `terminal:<hash>` ref is
printed to stdout -- capture it if you need to address that specific
terminal later (focus, move, close). Otherwise, run `inspect` to
recover refs at any time.

Shorthands accepted anywhere a ref is expected:

- A bare service name (`web`) expands to `service:web`.
- A bare `https://` URL is accepted as an `open` / `split` target
  (it creates a new ad-hoc URL tab); the optional `url:` prefix
  (`url:https://example.com`) works too.
- The literal `self` resolves to your own chat panel; most useful as
  `--relative-to=self` on `split` / `move`.

`subagent:` and existing `terminal:<hash>` / `url:<hash>` refs only
address panels that already exist (created via "New terminal" / "New
URL" in the UI, or by the subagent harness). You can't create those
from `open`.

## Less common operations

When `open` isn't enough, reach for one of these:

| Goal | Command |
|---|---|
| Place a new panel with explicit positioning | `python3 scripts/layout.py split <target> --relative-to=<ref> --direction=<left\|right\|above\|below> [--ratio=0.4] [--new-group]` |
| Focus an existing tab | `python3 scripts/layout.py focus <ref>` |
| Move an open tab next to another | `python3 scripts/layout.py move <ref> --relative-to=<ref> --direction=<dir> [--new-group]` |
| Rename a tab's label | `python3 scripts/layout.py rename <ref> "<title>"` |
| Maximize / restore a group | `python3 scripts/layout.py maximize <ref>` / `python3 scripts/layout.py restore` |
| Point an iframe at a new URL | `python3 scripts/layout.py replace-url <ref> service:<name>[/path]` |
| Reload one tab (or every iframe for a service) | `python3 scripts/layout.py refresh <ref>` |

`split` is the customization escape hatch: it accepts the same
targets as `open` plus full control over the anchor (`--relative-to`),
direction, size ratio, and whether to share an existing group or
carve a new one. Use it when `open`'s "to the right of your chat,
joining adjacent groups" default isn't what you want.

`open`, `split`, and `move` all default to **joining an existing
group** that already lives in the requested direction relative to the
anchor. Pass `--new-group` when you want a guaranteed fresh column
or row instead -- e.g. you want the new tab to take up its own
full-height column rather than tab into the iframe already there.

Output for `list` and `inspect` is YAML by default; pass `--json` for
programmatic consumption.

Run `python3 scripts/layout.py --help` (or `<subcommand> --help`) for
the full surface.

## Exit codes

`layout.py` uses distinct exit codes so wrapper scripts can branch:

- `0` ok
- `10` requested service not registered yet
- `11` couldn't reach the workspace server
- `12` HTTP error other than the codes below
- `13` mutex conflict -- another agent's layout op is in flight
- `14` not-found (the ref you named doesn't match an open panel)
- `15` bad request (malformed ref, unknown direction, unsupported URL)

## When NOT to use this skill

- **Building a brand-new web service.** Use `build-web-service` to
  scaffold the service first; it ends with a `layout.py open <name>`
  call to surface the new tab.
- **Persisting layout state.** The frontend auto-saves the layout on
  every change; you don't need to do anything special after a mutation.
