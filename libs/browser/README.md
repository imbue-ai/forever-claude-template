# browser

A per-workspace fleet of live Chromium browsers with a single atomic ownership
model: each browser is controlled by exactly one party at a time (a specific
agent, identified by its `MNGR_AGENT_ID`, or the human).

- **Daemon** (`browser-service`): a FastAPI service that owns every browser. Each
  browser is a headless Chromium driven by `browser_use.BrowserSession`, observed
  over the same CDP endpoint to stream a live view (`Page.startScreencast` ->
  base64 JPEG frames over a WebSocket) and inject human input. Browsers have
  monotonic integer ids; id 0 is the default and is (re)created on demand.
- **Ownership** is one locked, compare-and-set state machine per browser. Agents
  never preempt each other -- a second agent waits in a FIFO queue
  (monitor-and-wait). The human can take control from the UI at any time, which
  always wins and pins the browser to the human. For direct control ownership is a
  sticky lease (acquired on the first command, re-checked before every command, and
  auto-released when idle); for `task` it is bound to the live request connection.
- **CLI** (`agentic-browser-fleet`): the thin client the agent uses to drive the
  fleet. Primary path is **direct control** -- `state <id>` shows the page as a
  numbered list of clickable elements, then `open`/`click`/`input`/`scroll`/`keys`/
  `screenshot`/`tab` act on it (lifting browser-use's own executor against the live
  session). The agent does its own reasoning, so no API key is needed. `ls
  [--include-tabs]`, `new`, `acquire`/`release` round it out. An optional `task
  <id> "<goal>"` delegates a whole goal to an autonomous browser-use agent (the one
  path that needs a key). See the `agentic-browser-fleet` skill.
- **Viewer** (`assets/index.html`): a viewer-only page (no in-tab chat). It shows
  the live browser and, when an agent is driving, a grey "Agent has control"
  overlay with a "Take control" button; the agent's trace lives in the agent's
  output, not the tab.
- **Persistence**: the fleet survives a workspace stop/restart. Each browser gets
  its own persistent Chromium profile under `$MNGR_HOST_DIR/browser-profiles/`
  (Tier A -- on the workspace volume), so cookies/logins/history come back; Chromium
  does this itself, we just point `user_data_dir` at a durable dir. A tiny manifest
  (`runtime/browser-fleet.json`, Tier B -- git-backed to the mindsbackup branch)
  records which browsers existed and their tab URLs, so even a full rebuild restores
  the tab list (logged out, since profiles are volume-only). On daemon startup the
  fleet is restored **eager-sequentially** (one browser at a time, no cold-boot
  memory spike) behind an **init gate**: state-changing commands return a 503
  "initializing" until restore finishes, while `ls`/`state` stay open. A fresh
  workspace seeds browser 0 at the home page. `close <id>` retires a browser and
  forgets its profile; a crashed browser is never restored as healthy.
  - The profile dir name contains the literal `browser-use-user-data-dir-` substring
    on purpose -- it makes browser_use's `_copy_profile()` use the dir in place
    instead of copying it to a temp dir (which would silently defeat persistence).
    Pinned by `browser-use==0.13.1` and guarded by an integration test.
