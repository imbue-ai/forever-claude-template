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
  full); `close <id>` shuts an entire browser down (all its tabs) and retires its
  id -- distinct from `tab <id> close`, which closes a single tab.

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

- Take-control is now a true handoff. When a human takes a browser an agent was
  driving, the agent is queued to *resume* (rather than just stopped): its next
  command returns a clear "the human took control -- you're queued to resume"
  message, and the daemon messages the agent to pick up the moment the human hands
  the browser back (it re-reads the page with `state` and continues). This is the
  CAPTCHA / login handoff flow. Mechanics:

    - A human pin only *blocks* agents while the human is actively driving; if they
      go quiet for a grace period (`BROWSER_HUMAN_ACTIVE_GRACE`, default 120s -- long
      enough to read a CAPTCHA or fetch a 2FA code without the page being yanked, and
      any click/keystroke refreshes it) the pin yields: a queued agent is handed the
      browser automatically, and a freshly arriving agent simply takes it. So a
      forgotten hold never blocks the fleet. The "Return control to agents" button is
      the instant hand-back; the grace is only the walked-away backstop.

    - An agent handed the browser from the resume queue but that never sends a
      command (it was interrupted) has its grant revoked after a short claim window
      (`BROWSER_CLAIM_WINDOW`, default 12s), so the browser passes to the next waiter
      instead of sitting idle for the full idle-TTL on a no-show.

    - The resume queue is reported in the same `waiting` list the viewer and `ls`
      already show, and a rejected `busy_agent` command now also queues the agent to
      be woken when that browser frees.

- Browser-crash detection. If a browser's Chromium dies unexpectedly (OS/OOM kill,
  segfault), the daemon detects it -- via the Playwright observer's `disconnected`
  event, or lazily when a command finds the connection gone -- and marks the browser
  crashed instead of silently freezing. An agent's next command returns a clear
  "browser N crashed ... start a fresh one with `new`" (rather than a raw CDP
  exception); the live viewer tab shows a distinct "This browser crashed" state; and
  `ls` / `GET /browsers` report it. A crashed id is never reused (a new browser gets
  a new number, in its own tab), and crashed shells don't count toward the fleet cap,
  so a crash never blocks opening a new browser.

- The fleet now persists across a workspace stop/restart. Each browser gets its own
  persistent Chromium profile under `$MNGR_HOST_DIR/browser-profiles/` (on the
  workspace volume), so cookies/logins/history come back -- using Chromium's own
  profile persistence (a `user_data_dir`), not anything hand-rolled. A tiny manifest
  (`runtime/browser-fleet.json`, git-backed to the mindsbackup branch) records which
  browsers existed and their tab URLs, so even a full rebuild restores the tab list.
  On daemon startup the fleet is restored eager-sequentially (one browser at a time --
  no cold-boot memory spike) behind an init gate: state-changing commands return a 503
  "initializing" until restore completes, while `ls`/`state` stay open; the CLI maps
  that to a clear "still starting up, try again" message and the viewer shows a brief
  "restoring" banner. A fresh workspace seeds browser 0 at the home page. Closing a
  browser forgets its profile; a crashed browser is never restored as healthy.

- The viewer now ALWAYS shows a control indicator: a persistent "You have control"
  bar whenever you can drive the browser -- including a fresh, AI-untouched browser
  in its resting state -- not only after you explicitly take control. (Previously a
  resting browser showed no indicator at all.)

- Each browser the agent surfaces now opens as its OWN pane to the right (the layout
  split uses `--new-group`), instead of being tabbed into an existing browser pane.

- A non-primary agent (a `launch-task` sub-agent, or a second "+ New agent") that can
  reach the fleet daemon over the network but can't drive this workspace's layout no
  longer waits 5s and prints a confusing "service not registered / running headless"
  error when it tries to surface a pane. It now says plainly that the browser is
  running but its pane can only be shown by the primary agent (open it from the "+"
  menu), and the misleading "headless" wording is gone from the pane-pull failure path.
