- Dockview terminals are now persistent within the running container. Each
  "New terminal" tab is backed by its own named tmux session (`terminal-1`,
  `terminal-2`, ...), so a terminal survives closing the tab, reloading the
  workspace, and restarting the ttyd service. Only a container/host restart
  clears it (the tmux server is in-memory); a restored tab then reattaches, or
  comes back as a fresh shell if the session is gone.

- tmux is the source of truth: any session not prefixed with `MNGR_PREFIX`
  ("mngr-") is a user terminal and appears in the tab "+" menu to reattach to;
  `mngr-` sessions are agents and are not listed. Closing a tab detaches (the
  session keeps running); a new per-tab Destroy button kills the session (with
  a confirm dialog).

- Terminal tab titles track the live tmux session: switching sessions inside a
  terminal or renaming one updates the title, pushed over the existing
  WebSocket from tmux hooks.

- Each terminal panel shows a dismissable banner explaining this in-memory
  lifecycle (with "Never show again" persisted server-side) and linking to the
  persistence doc. Nothing typed or shown in a terminal is written to disk.

- New endpoints: `GET /api/terminals`, `POST /api/terminals/allocate`,
  `POST /api/terminals/<name>/destroy`, `GET`/`POST
  /api/terminals/banner-dismissed`, and the loopback `POST
  /api/terminals/notify` (fed by the tmux hooks).
