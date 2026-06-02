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

## The verbs you'll use 95% of the time

| Goal | Command |
|---|---|
| See what's currently open and how it's laid out | `python3 scripts/layout.py inspect` |
| Locate one panel + its tab-mates + cardinal neighbors | `python3 scripts/layout.py where <ref>` |
| List everything addressable (services + agents) with open/running flags | `python3 scripts/layout.py list` |
| Surface a service / URL / terminal / chat alongside your chat | `python3 scripts/layout.py open <target>` |
| Put a new terminal in the same tab group as your chat | `python3 scripts/layout.py split terminal --relative-to=self --direction=within` |
| Close a tab | `python3 scripts/layout.py close <ref>` |

`open` is the opinionated default. It puts the new tab to the right
of your chat, joining whatever group already lives there if one is
open. This *joining-existing-group* rule applies uniformly to every
`open` target -- terminals, service iframes, external URLs, and chats
alike. That can be surprising: `open terminal` with a service iframe
already to the right of your chat tabs the terminal *into that
iframe's group*, leaving a terminal nested next to an unrelated
iframe. Pass `--new-group` to force a fresh column instead.

Targets `open` accepts:

- A workspace service name (`web`) -- creates a new iframe for that
  service, or reports a no-op if one is already open (use `focus` to
  bring it to the foreground).
- `terminal` -- creates a fresh terminal in the primary agent's
  work_dir (each call adds a new one, just like the UI's "New
  terminal" button). The new tab's ref (`terminal:<hash>`) is printed
  to stdout so you can capture it for later ops.
- An external URL (`https://example.com`) -- creates a new ad-hoc URL
  tab, or reports a no-op if one is already open pointed at that URL.
- A chat ref (`chat:alice`) -- opens another mngr-level agent's chat.
- A `chat-terminal:<name>` ref -- opens (or focuses, if already open)
  the terminal attached to that agent's tmux session. Singleton: a
  second `open chat-terminal:alice` focuses the existing panel rather
  than creating a duplicate. This is the same terminal the chat
  panel's "Open agent terminal" button mounts.

## Refs: how every panel is addressed

Every panel has a stable, type-prefixed ref returned by `inspect`:

| Prefix | Meaning | Example |
|---|---|---|
| `service:<name>` | The iframe for a registered workspace service. | `service:web` |
| `chat:<agent-name>` | The chat tab for an mngr-level agent. | `chat:alice` |
| `chat-terminal:<agent-name>` | The terminal attached to that agent's tmux session. Singleton per agent. | `chat-terminal:alice` |
| `terminal:<short-hash>` | An anonymous terminal tab (created via `open terminal` / "New terminal"; each is a fresh instance). | `terminal:1a2b3c4d` |
| `url:<short-hash>` | An ad-hoc external URL tab. | `url:9f8e7d6c` |
| `subagent:<session-id>` | A harness-level subagent panel. | `subagent:abcd1234` |

When you call `open terminal`, the new tab's `terminal:<hash>` ref is
printed to stdout -- capture it if you need to address that specific
terminal later (focus, move, close). Otherwise, run `inspect` to
recover refs at any time. `chat-terminal:<name>` is stable by
construction, so you don't need to capture it: the same ref always
addresses the same agent's terminal.

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
| Place a new panel with explicit positioning | `python3 scripts/layout.py split <target> --relative-to=<ref> --direction=<left\|right\|above\|below\|within> [--ratio=0.4] [--new-group]` |
| Focus an existing tab | `python3 scripts/layout.py focus <ref>` |
| Move an open tab next to / into another's group | `python3 scripts/layout.py move <ref> --relative-to=<ref> --direction=<dir> [--new-group]` |
| Rename a tab's label | `python3 scripts/layout.py rename <ref> "<title>"` |
| Maximize / restore a group | `python3 scripts/layout.py maximize <ref>` / `python3 scripts/layout.py restore` |
| Point an iframe at a new URL | `python3 scripts/layout.py replace-url <ref> service:<name>[/path]` |
| Reload one tab (or every iframe for a service) | `python3 scripts/layout.py refresh <ref>` |

`split` is the customization escape hatch: it accepts the same
targets as `open` plus full control over the anchor (`--relative-to`),
direction, size ratio, and whether to share an existing group or
carve a new one. Use it when `open`'s "to the right of your chat,
joining adjacent groups" default isn't what you want.

### Directions on `split` and `move`

`--direction` takes five values:

- `left` / `right` / `above` / `below` describe the **adjacent group**
  in that cardinal direction relative to the anchor. By default, the
  new (or moved) panel tabs into a group that already lives there;
  pass `--new-group` to carve a fresh column / row instead so both
  panels are visible simultaneously.
- `within` describes the **anchor's own group**. The panel becomes a
  tab inside that group, alongside whatever is already there. This is
  the single-call form of "put X in the same group as Y". `--new-group`
  is meaningless with `within` and is rejected.

The most common natural request -- "put a new terminal in the same
tab group as my chat" -- is:

```bash
python3 scripts/layout.py split terminal --relative-to=self --direction=within
```

This creates the terminal and drops it as a tab inside the chat's
group, regardless of what else is to the right.

## Inspecting state

`inspect` defaults to a compact, one-line-per-group rendering:

```
active_panel: 1
row size=1.0
  [chat:alice* terminal:a1b2c3d4] size=0.4
  [service:web*] size=0.6
```

The `*` marks the active tab in each group. The header line names
the **arrangement** of each branch: `row` means children sit side by
side (left to right), `column` means they stack top to bottom.

Pass `--verbose` for the full YAML tree (including `panel_id`,
iframe URLs, and per-panel details) or `--json` for the structured
object (machine-readable, always full detail).

`where <ref>` zeros in on one panel. Compact default:

```
ref:    chat:alice
title:  alice
group:  [chat:alice* terminal:a1b2c3d4]
left    -
right   [service:web*]
above   -
below   -
```

`where --verbose` adds the full inspect tree under `full_layout`.

`list` outputs YAML by default; pass `--json` for programmatic
consumption.

Run `python3 scripts/layout.py --help` (or `<subcommand> --help`) for
the full surface.

## Mutating ops are synchronous

Every mutating op (`open`, `split`, `move`, `focus`, `close`,
`rename`, `maximize`, `restore`, `replace-url`, `refresh`) waits for
the resulting state to be observable via `inspect` before returning.
On success it prints a concise one-line diff on **stderr**:

- `opened service:web in tabs=[chat:alice*, service:web*]`
- `moved terminal:abc into tabs=[chat:alice*, terminal:abc]`
- `renamed chat:alice: 'alice' -> 'alice (lead)'`

On a **no-op** (the requested end state already holds), it prints
`no change: <ref> is already ...` to stderr and exits 0. This lets
you tell genuine success from "already done" without re-running
`inspect`.

`maximize`, `restore`, and `refresh` do not affect any
`inspect`-observable state, so they print
`(broadcast sent; no observable layout-state change to confirm)` on
stderr to make explicit that the op was sent without per-op
confirmation.

**stdout** is reserved for machine-readable output: the
server-allocated ref for `open terminal` / `split terminal` (so
wrapper scripts can capture it), and otherwise empty. Diffs and
no-op messages always go to stderr.

## Exit codes

`layout.py` uses three exit codes:

- `0` ok (including no-op successes)
- `1` error (anything failed -- the specific reason is in stderr,
  including the wait-stable timeout)
- `3` mutex conflict -- another agent's layout op is in flight (retry
  after a short backoff; the stderr message includes the in-flight
  holder's `agent_id`, `op`, `args`, `started_at`, and a suggested
  `retry_after_ms`)

## When NOT to use this skill

- **Building a brand-new web service.** Use `build-web-service` to
  scaffold the service first; it ends with a `layout.py open <name>`
  call to surface the new tab.
- **Persisting layout state.** The frontend auto-saves the layout on
  every change; you don't need to do anything special after a mutation.
