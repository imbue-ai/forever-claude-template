Opening a bare `service:browser` (with no `?session=<name>`) is now rejected with a clear message pointing at the right form, instead of spawning an orphan browser pane bound to no browser -- the dead "Open a browser from the + menu" placeholder. Browser panes must name a specific fleet browser; the fleet's own `new`/`task` commands already do, so only a stray manual `layout.py open service:browser` was ever affected.

The embedded browser-viewer iframe is now granted `clipboard-read`/`clipboard-write` permission so it can sync copy/paste with your local clipboard.
