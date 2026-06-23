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
