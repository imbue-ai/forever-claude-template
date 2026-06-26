Integrated the agentic browser fleet into the workspace UI.

- Fix: typing the name of a browser that already exists in the "New browser"
  modal no longer closes that existing browser's healthy pane. The modal now
  rejects a duplicate name inline ("A browser named <name> already exists")
  before opening any pane or calling create; and as defense in depth, a failed
  create only tears down the optimistic pane when this create actually opened a
  new one (never when it focused a pane that was already showing that browser).


- Browsers are now named, not numbered. Each browser in the fleet has a random
  ~2-word name (e.g. `alex-smith`), and that name is the addressing key
  everywhere (`service:browser?session=<name>`, the cast WebSocket, the pane
  title). There is no default browser; the fleet starts empty and every browser
  is created on demand.

- "New browser" now opens a name modal, mirroring "New agent": a dialog
  pre-filled with a random name (editable) where you can also type your own. On
  accept the browser pane opens immediately showing "Browser starting…" (an
  optimistic pane keyed by the chosen name) and connects once the browser is
  ready -- the launch is serialized server-side, so it may take a moment,
  especially while saved browsers are being restored. If the create is rejected
  (an invalid name, a duplicate name, the 3-browser cap, or Chromium still
  installing) the optimistic pane is closed and the daemon's exact message is
  shown inline in the modal (e.g. "3/3 browsers open -- close one first.") so you
  can fix the name and retry.

- "New browser" is no longer disabled during startup/restore -- it stays
  clickable at all times. A create issued while saved browsers are restoring is
  accepted and queued behind the restore (it just takes a little longer), so the
  earlier behavior that greyed out the item with a reason in parentheses while
  the fleet was starting up has been removed. The cap and duplicate-name checks
  now surface as inline modal errors instead of a pre-click gate.

- The "+" menu now lists the currently-active browsers (from the browser
  daemon's `GET /browsers`) alongside "New browser". Clicking an already-open
  browser focuses its existing pane instead of opening a duplicate.

- The agent-driven layout system can address a specific browser as a pane
  (`service:browser?session=<id>`), so an agent can pull the exact browser it is
  working on into a split-pane next to its chat, and panes for different browsers
  are treated as distinct (focus-if-open, no collisions).

- Updated the frontend to the browser daemon's new fleet endpoints (`/browsers`
  and `/browsers/{id}/cast`) in place of the previous single-session routes.

- "New browser" is no longer gated on an Anthropic API key. Direct control is
  keyless, so a browser can always be started; the old menu item that disabled
  itself and showed a "Browser sessions need an Anthropic API key" dialog was a
  leftover from the delegation model and has been removed.

- Dropped the placeholder "web" example server AND the "New URL" item from the
  "+" menu -- "New browser" is the real web surface / replacement for opening an
  ad-hoc URL. (The split-placement E2E that drove the old "New URL" item now
  exercises the same placement path via "New terminal".) Also removed the per-tab
  Refresh button from
  browser panes -- reloading the pane only reconnects the live view, which read
  as "restart the browser"; the browser viewer has its own in-page Reload button
  for the actual page.

- "New browser" is now gated on the fleet being ready to start one. The "+" menu
  reads `can_create` / `create_reason` from `GET /browsers`: while the fleet is
  still starting up / restoring saved browsers, or is at its cap, the item is shown
  disabled with the reason in parentheses (e.g. "New browser (browsers are still
  starting up)" / "New browser (5/5 open -- close one first)"), and clicking it pops
  a modal with the reason instead of firing a create that would fail. This prevents
  a "New browser" click during startup from racing the fleet's restore (e.g. while
  browser 0 or several saved browsers are still launching). Disabled "+" menu items
  no longer run their action on click (previously they were only greyed out).
