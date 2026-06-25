Integrated the agentic browser fleet into the workspace UI.

- The "+" menu now lists the currently-active browsers (from the browser
  daemon's `GET /browsers`) alongside "New browser". Clicking an already-open
  browser focuses its existing pane instead of opening a duplicate; "New browser"
  starts one and opens its pane, surfacing a clear error if the fleet is full.

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

- Dropped the placeholder "web" example server from the "+" menu (the browser
  fleet is the real web surface), and removed the per-tab Refresh button from
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
