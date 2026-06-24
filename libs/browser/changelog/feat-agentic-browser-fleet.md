Turned the single live-browser service into an agentic browser fleet that the
agent drives directly: a per-workspace daemon managing many headless Chromium
browsers, each with an atomic ownership state machine, plus an
`agentic-browser-fleet` CLI for the agent to drive them one command at a time.

- **Direct control (no API key):** the agent drives the browser itself --
  `state <id>` shows the page as a numbered list of clickable elements, then
  `open` / `click` / `input` / `select` / `scroll` / `keys` / `screenshot` /
  `tab` act on it (lifting browser-use's own action executor against the live
  session). The agent does its own reasoning, so no separate Anthropic key is
  needed; the live view streams to a minds tab the whole time. Indices come from
  the latest `state` and are re-queried after each change.

- Browsers have stable integer ids (0 is the default, created on demand; others
  are monotonic and never reused). `GET /browsers` / `ls [--include-tabs]` list
  the fleet with each browser's owner and tabs; `new` starts another (409 when
  full).

- Each browser is controlled by exactly one party at a time -- a specific agent
  (by `MNGR_AGENT_ID`) or the human -- via one compare-and-set transition guarded
  by a per-browser lock. For direct control this is a sticky lease: the first
  command acquires it and every command re-checks ownership right before acting,
  so a human "Take control" mid-sequence makes the agent's next command a clean
  "lost control" (resume only on the human's say-so via `--reclaim`); agents never
  preempt each other; an idle lease auto-releases. A human take-control always
  wins and pins the browser; "Return to agents" hands it back.

- The viewer tab is view-only: a grey "Agent has control" overlay + "Take
  control", a "Return to agents" affordance, and a "browser closed" state if the
  daemon restarts.

- An optional `task <id> "<goal>"` verb remains for whole-goal delegation to an
  autonomous browser-use agent (the one path that needs an Anthropic key); its
  ownership is bound to the live request connection.

- The Anthropic-key check (`anthropic_key_status`) now describes only the
  optional, key-only `task`/`extract` verbs -- it never gates starting or driving
  a browser (direct control is keyless), and its message reflects that rather
  than the old "Browser sessions need an Anthropic API key" wording.

- Direct control now surfaces the browser pane automatically: the first command
  for a browser (and the first after a human hands it back) splits it in as a pane
  to the right of your chat -- chat on the left, browser on the right, one pane per
  browser. Previously only the `task`/`lock` verbs pulled the pane in, so driving a
  browser with `state`/`click`/... left it headless. Re-acquiring an already-open
  browser just focuses its pane (no duplicates).

- Every tab now streams at the same resolution. browser-use pins the viewport on
  the first tab, but tabs opened later could come up at a different size and render
  with inconsistent letterboxing; the screencast now overrides the device metrics
  on each tab so they all stream at the fixed screencast size.

- The browser viewer's address bar gained Back / Forward / Reload buttons (active
  only while you hold control). Reload reloads the live page; it does not restart
  the browser.

- The "Agent has control" overlay now shows a live idle countdown -- e.g. "idle
  12s, releases control in 78s" -- so a watching human can see when a quiet agent's
  sticky lease will auto-release (the 90s idle-TTL). It also lists any agents queued
  (monitor-and-wait) behind the current owner. The same `waiting` queue is reported
  by `GET /browsers` and shown in `agentic-browser-fleet ls` (`[queued: ...]`).

- When you hold control, the bar now lists the agents queued to use that browser
  ("Agents waiting to use this browser: ..."), and the "Return control to agents"
  button only appears when one is actually waiting -- otherwise it reads "No agents
  are waiting" with no button (there is nobody to hand back to).
