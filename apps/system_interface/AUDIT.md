# System Interface — Comprehensive Code Audit

Audit of `apps/system_interface/` (Minds Workspace Server). Findings are grounded in the
current source on branch `gabriel/fix-workspace-server`. Each issue cites file:line and a
severity (CRITICAL / HIGH / MEDIUM / LOW).

The app is a FastAPI backend (`imbue/minds_workspace_server/`) that watches Claude Code
session JSONL files, manages `mngr` agents, and proxies HTTP/WebSocket traffic to
per-agent backend services. A TypeScript/Mithril frontend (`frontend/src/`) renders the
chat UI through Dockview, talking to the backend via REST, Server-Sent Events, and
WebSockets.

---

## 1. Top-priority issues (fix first)

These are the bugs and design flaws most likely to cause production incidents:

1. **Proxy buffers full request and response bodies** (`service_dispatcher.py:84, 129, 174-206`,
   `proxy.py:91`). A multi-GB upload or download through `/service/<name>/...` OOMs the
   server *and* the browser SW. No max-body cap anywhere. Use `request.stream()` plus
   `httpx` streaming and add a hard size guard.

2. **Watchdog file watcher loses bytes at JSONL/UTF-8 boundaries**
   (`session_watcher.py:362-380`). `byte_offset` is advanced by raw byte count and the
   tail is decoded with `errors="replace"`. A read that lands mid-line or mid-codepoint
   silently drops events forever. Must scan back to the last `\n` before advancing
   `byte_offset` and reset on file truncation/rotation (`current_size < byte_offset` is
   not handled — watcher goes mute until the file grows past the old offset).

3. **SSE generator runs in a sync `StreamingResponse` and pins a threadpool worker per
   subscriber** (`server.py:299-337, 369-409`). `queue.get(timeout=1)` inside a `def`
   generator means N concurrent SSE clients permanently hold N threadpool workers
   (default ~40), starving every other `run_in_threadpool` call (screen capture,
   destroy, layout save, the WS broadcast loop). Convert to `async def` polling an
   `asyncio.Queue`.

4. **Unbounded in-memory growth across the server**: `_existing_event_ids`,
   `_tool_name_by_call_id`, `_session_states`, `_subagent_metadata` in
   `session_watcher.py:71-76`; `event_queue` (no maxsize) and per-agent
   `_event_buffers` in `event_queues.py:34, 63`; `eventsByAgent` and `notFoundAgentIds`
   on the frontend (`Response.ts:71-72`). Long-running sessions or destroyed agents
   are never evicted. Memory grows monotonically.

5. **Side effects fired from inside Mithril `view()`** (`ChatPanel.ts:165-168, 314-318,
   365`; `DockviewWorkspace.ts:962-972`). `view()` opens WebSockets, schedules
   layouts, kicks backfill loops, and calls `m.redraw()` — all of which can fire on
   every redraw. The current code only behaves because of fragile `currentAgentId` /
   `backfillStarted` guards. Move to lifecycle hooks (`oninit`, `onupdate` with proper
   change detection, `onremove`).

6. **WS proxy `gather()` never cancels the surviving direction**
   (`service_dispatcher.py:276-323`). When the backend WS dies first,
   `_forward_client_to_backend` stays blocked on `receive()` indefinitely. Use
   `asyncio.wait(..., return_when=FIRST_COMPLETED)` and cancel the loser. Also
   `data.get("text") is not None` is the correct check, not `"text" in data` (Starlette
   always sets both keys, one to `None`).

---

## 2. Backend: HTTP / WebSocket / Proxy layer

### CRITICAL

- **Full-body buffering in the proxy** — `service_dispatcher.py:84, 129, 174-206`,
  `proxy.py:91`. See top-priority #1.
- **SSE generators are sync** — `server.py:299-337, 369-409`. See top-priority #3.
- **Cross-loop cancel race in the WebSocket broadcaster** — `ws_broadcaster.py:124`.
  `register()` captures the calling task and uses `loop.call_soon_threadsafe(task.cancel)`
  to evict stuck handlers, but `broadcast()` is called from a background thread per the
  docstring. If `AgentManager` (server.py:76-78) is reused across apps the captured
  loop may have closed; the resulting `RuntimeError` is logged and the handler leaks.
  `_handler_by_id` is also unbounded if `unregister` is skipped on handler crash.

### HIGH

- **`event_generator` is duplicated** across `_stream_events` and
  `_stream_subagent_events` (server.py:299/369) — 90% identical, two places to fix
  every bug.
- **Unbounded layout body and blocking disk I/O on event loop** — `server.py:454, 463`.
  `await request.body()` with no cap, then sync `write_bytes` inside an `async` handler.
- **Single shared `httpx.AsyncClient`** with a 30 s blanket timeout
  (`server.py:96-99`) — kills legitimately long backend SSE streams.
- **`rewrite_absolute_paths_in_html` regex misses CSS `url(...)`, srcset, inline-style
  backgrounds, meta-refresh** — `proxy.py:170-186`. Service-worker covers fetches at
  runtime so blast radius is limited.
- **Cookie path rewrite is fragile** (`proxy.py:7, 143-153`): doesn't handle multiple
  `Path=` instances, comma-joined `Set-Cookie`, or `Path` with no value.
- **WebSocket proxy never cancels survivor on backend death** — see top-priority #6.
- **Binary/text confusion in WS proxy** — `service_dispatcher.py:289-291`. `"text" in
  data` is always `True`; must check `data.get("text") is not None`.
- **`ws_broadcaster.broadcast()` holds `_lock` across all client enqueues**
  (`ws_broadcaster.py:95-110`) — serializes every broadcast against every connect.

### MEDIUM / LOW

- **SIGINT handler runs blocking shutdown inline** (`server.py:104-120`) — risks
  deadlock if any lock is held by the interrupted code. Use
  `loop.add_signal_handler` and a flag.
- **Global exception handler leaks `str(exc)` to clients** (`server.py:770-777`).
- **No validation on `service_name` used as cookie name** —
  `service_dispatcher.py:55-56`.
- **`__sw.js` short-circuit misses `?v=` query strings** —
  `service_dispatcher.py:226-227`.
- **`shutdown()` doesn't cancel wedged handler tasks** —
  `ws_broadcaster.py:177-186` (relies on every handler hitting the `None` sentinel).
- **`event_queues.is_shutdown` polling is redundant** with the sentinel
  (`server.py:310, 380`).
- **No auth on WS/HTTP proxy surface** — relies on loopback binding; should be
  asserted in code or documented at the entrypoint.

---

## 3. Backend: agent state, file watching, parsing

### CRITICAL

- **Unbounded watcher state** — `session_watcher.py:71-76, 133-135, 386-389`. See
  top-priority #4. `_known_session_ids` (line 72) is appended to but *never read* — dead
  storage that still grows.
- **Full file re-read on every backfill page** — `session_watcher.py:120-136`.
  `state.file_path.read_text().splitlines()` per call, re-parsed end-to-end through
  `parse_session_lines`. Backfill paginating a long session is O(n) per page and
  blocks the calling thread on multi-MB I/O.
- **Event queue has no backpressure** — `event_queues.py:34, 63`. `queue.Queue()` with no
  maxsize plus per-agent `_event_buffers` that retain every `STORE` event forever.
  A slow SSE consumer is an unbounded memory leak.

### HIGH

- **Watchdog `Observer.schedule` accumulated per discovery call** —
  `session_watcher.py:250-255, 289-293`. Multiple `_ChangeHandler` instances for the
  same parent dir. Each filesystem event then fires N callbacks.
- **`time.sleep(_BRIEF_WAIT_SECONDS)` on the watcher hot path** —
  `session_watcher.py:239`. With many pending sessions this stalls the loop.
- **File truncation/rotation not handled** — `session_watcher.py:362-376`. Watcher
  goes silent if `current_size < byte_offset`.
- **UTF-8 boundary corruption** — `session_watcher.py:380`. See top-priority #2.
- **Cross-thread mutation of `_session_states` without locking** —
  `session_watcher.py:71, 108, 245, 273, 350`. Watcher thread mutates while HTTP
  handlers iterate `.values()`. `RuntimeError: dictionary changed size during iteration`
  will appear intermittently.

### MEDIUM / LOW

- **Silent `except OSError: pass` swallows real failures** —
  `session_watcher.py:286-287, 292-293, 330-331, 345-346, 357, 377-378`.
- **`_handle_observe_output_line` raises into watchdog thread** —
  `agent_manager.py:732`. Parser violation silently halts observe-event processing.
- **`tomllib.loads(toml_path.read_text())` on watchdog thread** —
  `agent_manager.py:869`. Blocking I/O in the dispatcher; broadcaster call at
  line 862 also races with locked state read.
- **`os.walk` per missing-session lookup** — `session_watcher.py:303-306`. Whole
  `projects/` tree walked on every discovery.
- **No `AgentSessionWatcher` lifecycle hook on agent destruction** —
  `agent_manager.py:796` only stops `_app_observers`. Watchers may leak; needs
  verification.
- **`_completion_signal_put` blocking 5 s per signal** —
  `agent_manager.py:67-87`. Slow shutdown amplifier.
- **`parse_session_lines` silently swallows `JSONDecodeError`** —
  `session_parser.py:86-93`. Corrupt sessions invisible to operators.
- **`request_writer.py:43`: append-only events file never rotated** — grows forever.
- **Dead/unused: `_refresh_agents` (`agent_manager.py:579`); `_known_session_ids`
  (`session_watcher.py:72`).**

---

## 4. Frontend: Mithril / Dockview / streaming UI

### CRITICAL

- **Side effects in `view()`** — `ChatPanel.ts:165-168, 314-318, 365`. See
  top-priority #5.
- **`m.redraw()` from a path that started inside `view()`** —
  `ChatPanel.ts:239`. Only safe because of `await`; brittle.
- **`dockview.layout()` on every Mithril update** —
  `DockviewWorkspace.ts:962-972`. Should be a `ResizeObserver` on the container.
- **`alert()` for destroy errors** — `DockviewWorkspace.ts:934, 938`. Blocks the event
  loop, breaks in sandboxed iframes, and is wrong UX. Surface inline.

### HIGH

- **AgentManager WS: `JSON.parse` without try/catch; `onerror` silently closes; no
  user-visible disconnect indicator** — `AgentManager.ts:79-94`.
- **Reconnect loops with no exponential backoff** — `StreamingMessage.ts:54-59`,
  `AgentManager.ts:54, 101-104`. Hammers the backend forever when it's down.
- **`fetchEvents` overwrites `eventsByAgent[agentId]` wholesale** —
  `Response.ts:126`. Concurrent `appendEvents` writes from a live SSE are lost on a
  refresh-while-streaming agent switch.
- **`MessageInput.sendMessage` errors silently swallowed** —
  `MessageInput.ts:53-57`. Message removed from localStorage and UI, but if the POST
  failed the user has no idea.
- **First-connect drop window in `StreamingMessage`** —
  `StreamingMessage.ts:50-61`. Events generated between EventSource close and
  snapshot fetch can be lost unless server-side cursoring covers the gap.
- **`m.mount(element, null)` may race with async dockview detach** —
  `DockviewWorkspace.ts:115-117`. EventSources leak if `onremove` doesn't run in time.

### MEDIUM

- **`markdown.ts:83-89`: full reparse + `innerHTML` wipe on every `onupdate`** —
  jank on long streaming messages. Memoize on `content`.
- **`iframe sandbox="allow-scripts allow-same-origin"`** — `IframePanel.ts:18`.
  Combination is effectively no sandbox for same-origin documents. Intentional?
- **`showCustomUrlDialog` uses raw `innerHTML` template with no listener removal** —
  `DockviewWorkspace.ts:542-552`. Static HTML so no XSS, but inconsistent with the
  rest of the codebase.
- **`ProtoAgentLogView.ts` is entirely dead code** — duplicated in `ChatPanel.ts:111-163`.
- **Unbounded `logLines`, `events`, `eventsByAgent`, `notFoundAgentIds`** —
  `ChatPanel.ts:139, 187`; `Response.ts:71-72`. No eviction when agents are
  destroyed; no virtualization for long transcripts.
- **`SubagentView` recomputes `toolResults` Map every redraw** —
  `SubagentView.ts:140-145` (same in ChatPanel:367). Memoize against event-list
  identity.

### LOW

- **`Response.ts:185-203`, `StreamingMessage.ts:99-117`, `Conversation.ts`** — half the
  exports are no-op compatibility shims. Delete in a follow-up.
- **`llm-api.ts:42-50`: `getResponse` does O(N×M) over an empty map.** Dead path.
- **`message-renderers.ts:163`: `href="javascript:void(0)"`** — should be a `<button>`.
- **`MessageInput.ts:109-111`: `m.trust()` of inline SVG** — static literal, safe; risky
  pattern to copy.
- **`ChatPanel.ts` is 441 lines mixing five concerns** — split.
- **Global click listener for the empty-state overlay** —
  `DockviewWorkspace.ts:739`. One-time today; leaks on hot-reload.

---

## 5. Cross-cutting themes

These patterns recur across files and are worth addressing structurally:

1. **No backpressure anywhere in the streaming path.** Producer (file watcher /
   broadcaster) → queue → SSE consumer is all unbounded. A slow client becomes a
   memory leak. Add bounded queues with a documented overflow policy (drop oldest,
   reset client, etc.).
2. **Blocking I/O on the asyncio event loop.** Sync `write_bytes`, `read_text`,
   `tomllib.loads`, and `queue.get(timeout=1)` appear inside `async def` functions or
   the threadpool. Standardize on `run_in_threadpool` or `aiofiles` for disk and
   `asyncio.Queue` for cross-task signaling.
3. **Errors swallowed silently.** Bare `except OSError: pass`, `except Exception:`
   without logging, fire-and-forget fetches in the frontend, `JSON.parse` without
   try/catch. Operators have no signal when ingestion stops working.
4. **No lifecycle eviction.** Destroyed agents leave behind queues, buffers, event
   lists, watcher state, and frontend caches. Long-running deployments leak
   monotonically.
5. **`view()` doing work.** Mithril views must be pure. Today, every `ChatPanel`
   redraw can open WebSockets, kick async loops, and call `m.redraw()`. The current
   guards mask the problem; the next change will reintroduce a duplicate-connection
   bug.
6. **Dead and shim code.** `ProtoAgentLogView`, half of `Response.ts` /
   `StreamingMessage.ts` / `Conversation.ts`, `_refresh_agents`, `_known_session_ids`,
   the second duplicated SSE generator. Net deletion would meaningfully reduce
   surface area.
7. **Compatibility-only test exposure.** Worth a follow-up review to confirm none of
   the dead exports have test-only callers that should also be cleaned up.

---

## 6. Suggested fix order

1. Streaming bodies + max-body guard on the proxy (#1) — biggest production risk.
2. Watcher correctness: JSONL boundary + rotation + cross-thread lock (#2 plus
   `session_watcher.py:71`).
3. Convert SSE generators to async (#3); collapse the duplicate.
4. WS proxy `wait(FIRST_COMPLETED)` + text/bytes detection fix (#6).
5. Bound the event queue / per-agent buffers and add eviction on agent destroy (#4).
6. Move side effects out of `view()` and wire `onremove` cleanly (#5).
7. Reconnect backoff (`StreamingMessage.ts:54-59`, `AgentManager.ts:54, 101-104`).
8. Sweep silent excepts; add logging.
9. Delete the shim/dead code in `Response.ts`, `StreamingMessage.ts`,
   `Conversation.ts`, `ProtoAgentLogView.ts`.
