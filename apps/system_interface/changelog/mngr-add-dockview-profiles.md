Add named dockview layouts to the workspace.

The single implicit `layout.json` is replaced by named layouts stored as separate JSON files (`workspace_layout/layouts/<slug>.json` plus a `layouts_meta.json` registry). Two defaults, `desktop` and `mobile`, always exist; an existing `layout.json` is migrated into `desktop` on first access.

Each browser client picks its layout on first connect by user agent (mobile vs desktop), remembers the choice in localStorage, and autosaves only into its active layout. The "+" menu gains a bottom section with "Save layout...", "Load layout...", and "Delete layout..." dialogs (the active layout is marked "(current)"; saving under a new name switches to it; the last remaining layout cannot be deleted).

Live cross-client sync: when a client saves a layout, other clients with it active re-apply it; deleting a layout switches affected clients to the first remaining one.

Chat messages sent through the UI and every layout switch are recorded in `workspace_layout/events/client_activity/events.jsonl` (client id, device kind, layout), and the layout-op broadcast endpoint gains `context` (per-client summary) and `load` (switch a client onto a layout) ops. Mutating layout ops are now layout-targeted: they require a target layout and only apply on connected clients that have it active.

Fix a chat/terminal pane restored by a layout switch binding to the wrong agent: dockview disposes the outgoing panels before creating the incoming ones, and the removal handler deleted their bookkeeping entries, so a pane present in both layouts (panel ids are deterministic) came back with no params and silently fell back to the primary services agent -- the tab kept its title but showed an empty transcript. The layout apply now tears the old layout down before seeding the new params, and a panel whose params are missing is rebuilt from its panel id (repairing already-corrupted layout files on the next save) instead of defaulting to another agent; a panel whose identity genuinely cannot be recovered now says so instead of rendering a plausible-looking wrong pane.
