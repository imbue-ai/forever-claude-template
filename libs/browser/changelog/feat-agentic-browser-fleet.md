Two viewer fixes surfaced in live testing:

- A newly-created browser's pane no longer stays stuck on "Starting browser…"
  after the browser is up. The viewer used to clear that banner only on the first
  screencast frame, but a browser sitting on a static/blank page sends a
  `control`/`tabs` sync without ever emitting a frame to a late-connecting client,
  so the banner never cleared (reopening the tab fixed it). The pane now self-heals:
  the viewer clears the banner as soon as it gets a `control` or `tabs` message
  (evidence the browser is live), and the daemon replays the last screencast frame
  to a newly-connected cast client so the canvas shows the live page at once instead
  of staying black. The 1013 "starting" retry and the terminal 1008 "closed" states
  are unchanged.

- The browser-crash / OOM state is now a full, clearly-visible overlay: a
  near-opaque grey cover over the whole pane with large white "This browser
  crashed" text and a lighter "Open a new browser from the + menu" hint -- instead
  of the previous faint line of grey text on the black canvas.

----

Follow-up fixes on the named-fleet work:

- The `GET /browsers` route read the fleet's live count directly on the Flask
  worker thread, which could race the loop thread mutating the fleet and
  intermittently error mid-iteration; that read now runs on the loop thread like
  every other fleet-state access, and the live-browser snapshot is iteration-safe.
- A browser closing exactly while a tab re-attached its screencast no longer logs
  a harmless "Task exception was never retrieved" traceback.
- The library README was corrected to the named-fleet model (random ~2-word
  names, empty startup fleet, no default browser) -- it had stale "integer id /
  id 0 is the default" wording.

----

Browsers are now addressed by a random ~2-word english NAME (like a mngr agent
name, e.g. `alex-smith`) instead of a sequential integer id, and the fleet starts
EMPTY -- there is no default browser. Every browser is created on demand.

- **Names, not numbers.** The name is the addressing key everywhere: the CLI
  `<name>` argument, `service:browser?session=<name>`, the cast WebSocket path
  `/browsers/<name>/cast`, the manifest, and the persistent profile dir
  (`browser-use-user-data-dir-<name>`). `new` picks a random name (printed as
  `started browser alex-smith`); pass `new <name>` to choose one. A user-typed name
  must be lowercase letters/digits joined by single dashes (1-40 chars); an invalid
  name is rejected (`POST /browsers` -> 400), and a duplicate of a live browser is
  rejected (-> 409). Names are unique within the live fleet (regenerated on collision)
  and never reused. Browsers CANNOT be renamed -- there is no rename verb.

- **No default browser; empty fleet at startup.** The reserved browser-0 and the
  monotonic id counter are gone. A fresh workspace restores to an empty fleet; run
  `new` to open one. `GET /browsers` no longer materializes a default. The
  daemon-internal `ensure_browser_0` path was removed.

- **Cap is now 3 (was 5).** `new` past the cap is rejected (not queued) with the exact
  message `3/3 browsers open -- close one first`. `BROWSER_MAX_SESSIONS` still overrides.

- **Create works DURING restore.** The init gate no longer blocks `POST /browsers`:
  a create issued while the fleet is still restoring is accepted and simply queues
  behind the serialized relaunches (one Chromium launches at a time, on the shared
  manager lock -- the OOM guard is preserved). Only the drive verbs (task/click/...)
  still 503 "initializing" during restore; `ls`/`state` and `new` work throughout. The
  "New browser" readiness no longer gates on init -- only on Chromium install + the cap.

- **HTTP contract change:** `POST /browsers` accepts an optional body `{"name": "<name>"}`
  and returns `{"name": <chosen-name>, "key_available": <bool>}` (was `{"id": ...}`).
  All `/browsers/<id>/...` routes now take the name as a string path segment.

- **Optimistic 'starting' pane support.** When the viewer opens a pane for a name whose
  browser hasn't registered yet (the optimistic pane opened on modal-accept, before the
  serialized launch finishes), the cast WS closes with code 1013 ("Try Again Later") for
  a syntactically-valid-but-unknown name, and the viewer shows "Browser starting..." and
  retries with backoff -- connecting once the launch registers the name. An invalid/gone
  name still closes 1008 (terminal). The viewer addresses the pane by name (the old
  numeric `?session=` parse that silently defaulted to browser 0 is gone).

- **Manifest format v2.** Entry ids are strings and the `next_id` high-water mark is
  removed. `read_manifest` now rejects any non-current version, so an upgrade across the
  int->name change starts from an empty manifest and re-scans profiles; legacy numeric
  profile dirs (`browser-use-user-data-dir-0`/`1`/`2`) are skipped on scan (not relaunched
  as bogus named browsers) and swept.

----

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

    - A human take-control is STICKY: it holds until the human explicitly hands back
      ("Return to agent"), with no idle/grace yield. A human who grabs a browser keeps
      it even if they step away mid-CAPTCHA/login, so they never come back to find an
      agent moved the page out from under them. (Agents still auto-release via the idle
      lease -- the asymmetry is deliberate: a dead agent must not hoard a browser, a
      human must never be force-yielded. The tradeoff is that a forgotten human hold
      parks that one browser until released; other browsers and `new` are unaffected.)

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

- `new` now opens the browser's pane immediately (idempotent with the pane-pull the
  first direct command also does), so "open a new browser" visibly opens one rather
  than showing nothing until the first `open`/`click`.

- Any agent the user started -- the primary, or one opened via "+ New agent" --
  surfaces the pane next to its OWN chat. A `launch-task`/background agent (no chat in
  this workspace's UI) can't land the split; instead of leaking layout.py's raw 5s
  "service not registered" error (and the misleading "headless" wording), it now warns
  in one clean line that the browser is running but a background agent can't show panes
  (open it from the "+" menu, or have the main agent drive it).

- Agent-initiated handoff for CAPTCHAs / human verification. A new
  `agentic-browser-fleet handoff <id> "<reason>"` verb (alias `request-human`) lets an
  agent that hits a wall only a human can clear -- a CAPTCHA, a "verify you're human"
  challenge, an SMS/2FA code, a credential login -- hand the browser to the human and
  stop. The agent is placed at the FRONT of that browser's resume queue (it's mid-task)
  and control goes to the *human*, pinned (not passed to the next queued agent), until
  the human hands it back -- at which point the requester is the first agent woken to
  resume. The viewer shows a distinct amber bar naming the agent and what to do ("X
  needs your help: solve the CAPTCHA ... then click Return to agent"), and the pane is
  surfaced/focused. The skill instructs agents to use it (and to NOT attempt CAPTCHAs
  themselves). Exit code `2` (preempted), so the agent stops and ends its turn.

- Browser-sandbox portability across minds modalities. Chromium's in-process sandbox
  cannot start as root on a plain-Linux runtime ("Running as root without --no-sandbox
  is not supported"), and browser-use turns that into a ~30s launch hang -- which was
  surfacing as "Failed to create a browser: HTTP 504" on Lima (a bare Debian VM, where
  the daemon runs as root and there's no gVisor). Since every minds workspace runs the
  daemon as ROOT inside an OUTER boundary (gVisor under docker/cloud/AWS, the VM under
  Lima/Vultr) that already contains the browser, the daemon now disables Chromium's inner
  sandbox whenever it runs as root (the reliable signal -- browser-use's own IN_DOCKER
  check misses the bare-VM case), and keeps it for a non-root runtime where it works.
  `BROWSER_NO_SANDBOX=1` forces it off regardless; a sandboxed launch that still fails
  retries once without it. No provider sniffing. (Verified on a live Lima workspace:
  browsers launch with `--no-sandbox` and `POST /browsers` returns 200.)

- "New browser" is gated on fleet readiness so the startup/restore race is handled
  gracefully. `GET /browsers` now reports `can_create` / `create_reason` / count / max,
  mirroring exactly what a create would do: it's false (with a reason) while the fleet is
  still starting up or restoring saved browsers, or when at the cap. The init gate already
  refuses a create during restore (so a "New browser" can never pile a concurrent launch
  onto a fleet relaunching browser 0 / multiple saved browsers); this just surfaces the
  reason instead of a bare 503.

- The browser fleet daemon was migrated from FastAPI/uvicorn (async) to Flask +
  flask-sock (synchronous, thread-per-connection), with browser_use's async quarantined
  behind one background asyncio event loop reached via a single
  `run_coroutine_threadsafe` bridge. The per-browser ownership state machine keeps its
  asyncio locks/events unchanged on that one loop, so every ownership guarantee (atomic
  single owner, the compare-and-set, FIFO wait/resume queues, human-always-wins
  take-control, idle-TTL release, captcha handoff, disconnect-as-lease, the 503 init
  gate) is preserved. The screencast WebSocket and direct-control HTTP API are unchanged;
  there are no user-visible API or viewer changes.

- Hardened the post-migration ownership handling so a human "Take control" can never be
  preempted by a `task` run that started a beat too late. After the Flask split, the
  endpoint acquires the browser in one coroutine and starts the browser-use run in a
  separate one; a human take-control in that gap previously cancelled nothing (the run's
  cancellable handle wasn't registered yet) and the agent then drove a browser the human
  owned. The run now registers its handle and re-checks ownership together under the
  control lock before driving, and aborts with "lost control" if the human (or an idle
  sweep) took the browser first -- so the human always wins this race. The idle-lease
  sweep now snapshots the control fields under the same lock, and the daemon's
  startup-status read/write is lock-guarded for the Flask reader threads.

----

Reworked browser startup around an explicit per-browser lifecycle so a freshly-created
browser's viewer pane is deterministic instead of racing the launch:

- Each browser now tracks an explicit lifecycle: `init` (registered, Chromium not up
  yet) -> `running` (Chromium up + screencast attached) -> `crashed`. It is reported on
  every cast `control` broadcast, in each direct-command response, and in the `GET
  /browsers` entry, so the whole system reads one source of truth instead of inferring
  state from whether a frame arrived.

- `POST /browsers` (and the `new` CLI verb) now returns the browser's name IMMEDIATELY:
  `create` registers the browser `init` under the registry lock (the cap counts `init`
  browsers too) and kicks the Chromium launch off as a background task. The launch is
  serialized by a dedicated startup lock (at most one Chromium starting at a time), so
  multiple new browsers queue and boot back-to-back. When a browser comes up it flips to
  `running` and broadcasts; if its launch fails it is removed (not left as a stranded
  `init` shell) and a `launch_failed` is broadcast. Restore uses the same
  register-init -> serialized-launch path. Only `running` browsers are persisted to the
  manifest.

- A direct command (`state`/`click`/...) or a `task`/`lock`/`acquire` on a browser that
  is still `init` now returns a clear, non-fatal "still starting up (Chromium is
  launching) -- try again in a few seconds" (exit 3) so the agent waits and retries
  instead of erroring on a half-built browser. Driving/ownership applies only once
  `running`.

- The viewer renders deterministically off the lifecycle: `init` shows a full
  "Starting browser…" overlay covering the whole pane (tab/nav chrome hidden, no
  half-built browser), `running` shows the live page (and removes the overlay on the
  init->running transition), `crashed` shows the crashed overlay. The 1013 retry is now
  just a thin fallback for the brief pre-registration window; a `launch_failed` is
  terminal like 1008.

Hardened the new background-launch path against teardown races:

- `close()` racing a suspended launch can no longer resurrect a removed browser or leak a
  second Chromium. `start()` now re-checks `_closed`/`_crashed` after the Chromium starts
  and again right before the `running` flip, killing the just-launched Chromium and
  aborting on that early return; and `manager.close` serializes against the in-flight
  launch (awaiting its task) so the teardown lands after the launch finishes or aborts.

- Cast sockets now tear down deterministically: `close()` and the launch-failure path push
  a shutdown sentinel onto each connected cast queue (the launch_failed message is sent
  first), so the server-side cast thread exits on its next drain instead of waiting for the
  client to disconnect.

Code-review fixes to the daemon (runner.py / session.py):

- A client parked in the acquire FIFO wait-queue that disconnects is now detected
  promptly. The wait-phase NDJSON stream emits a heartbeat `ping` on each idle poll (like
  the run/hold loops already did), so a dropped waiter surfaces as a broken-pipe within a
  poll interval and its acquire is cancelled and the waiter removed -- instead of a dead
  waiter holding its slot for the holder's whole lease (up to ~15 min) and blocking
  everyone behind it.

- A human "take control" is now gated on lifecycle: it no-ops on an `init` browser
  (Chromium not up yet) or a `crashed` one, and only pins once the browser is `running`.
  Previously taking control of a still-launching browser pinned it before it came up, so
  it booted locked to the human and blocked every agent.

- The `acquire` and `handoff` direct-control responses now read their owner-state snapshot
  ON the background loop (in the same coroutine as the mutation) rather than off the Flask
  thread, so the returned status and the embedded `controller`/`owner`/`lifecycle` fields
  are always a single consistent view.

- A newly-created browser is now persisted to the manifest the moment it is registered
  (`init`), not only after it reaches `running`. A daemon crash during the multi-second
  Chromium launch no longer loses a browser the user just asked for; it is restored next
  boot (an `init` browser has no tabs yet, so it restores to the home page). The durable
  manifest now snapshots the live fleet (init + running); crashed shells are still
  excluded. (This supersedes the earlier "only running browsers are persisted" note.)

- A fresh viewer of a `running` browser that has not repainted (so no screencast frame has
  been cached yet) now gets a one-off captured frame on connect, so the live page shows
  immediately instead of a black canvas until the next repaint.

- A viewer that joins an already-`running` browser is no longer told the fleet is
  "initializing" while the rest of the fleet is still restoring -- the initializing banner
  is now lifecycle-aware (its seed already carries `lifecycle=running` and the live page is
  streaming).

- A browser whose background launch FAILED is remembered briefly, so a late/retrying
  optimistic viewer (one still in 1013 reconnect-backoff when the launch failed, which
  never registered a cast queue and so missed the `launch_failed` broadcast) is now closed
  terminally (1008) instead of retrying forever.

- Direct-control browser actions (navigate/click/input/.../tab/state/screenshot) now run
  under a generous dedicated timeout (default 600s, env `BROWSER_DIRECT_ACTION_TIMEOUT`,
  0 = no timeout) instead of the 120s route timeout, so a slow-but-legitimate action on a
  heavy page is not cancelled mid-flight. A timeout cancellation is still safe for the
  ownership state machine: the lease is set before the action and stays held, and no
  ownership field is left half-written.

Two follow-up fixes from the adversarial re-review of the above:

- The acquire wait-phase disconnect path now releases as well as cancels. If a client drops
  in the same poll window a grant lands on the loop, the wakeup beats the cancel (the
  acquire runs straight through to "acquired"), so the cancel hits an already-done task and
  the just-granted lease would otherwise sit orphaned -- no run task, dead connection --
  until the 90s idle sweep, blocking everyone queued behind it. The disconnect path now also
  calls `release`, a CAS no-op unless that orphaned-grant case actually occurred. Mirrors the
  run/hold finally.

- The `acquire` verb is now strictly non-blocking. It is the fast reserve-or-queue verb (a
  busy browser enqueues the agent to be woken when it frees and returns immediately);
  blocking-wait lives in `task`/`hold`, which heartbeat and so detect a dropped client.
  Honoring `wait=true` on this non-streaming endpoint could have pinned a worker thread and a
  queue slot indefinitely on a caller that walked away.
