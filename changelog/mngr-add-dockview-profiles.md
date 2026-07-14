Named dockview layouts: `scripts/layout.py` mutating ops now require `--layout <name>` and only apply on connected clients with that layout active; `inspect` / `where` / `list` accept an optional `--layout` (defaulting to the last active layout).

New subcommands: `layout.py context` (per browser client: device kind, current layout, connection state, recent messages -- for attributing a request to a client/layout) and `layout.py load <layout> [--client <id>]` (switch a client onto a named layout so subsequent ops can target it).

The `manage-layout` skill documents the layout-targeting workflow, including the fallback guidance for messages that arrive without client metadata (e.g. via tmux).
