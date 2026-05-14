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

1. **Proxy buffers full request and response bodies** (`service_dispatcher.py:84,
   129, 174-206`, `proxy.py:91`). The HTTP proxy path calls
   `await request.body()` to read the full client body before opening the
   `httpx` request, and reads `backend_response.content` / `.text` to assemble
   the response — both sides fully in memory. A multi-GB upload through
   `/service/<name>/...` OOMs the server. The browser service worker is the
   same pattern: `init.body = await event.request.arrayBuffer()`
   (`proxy.py:91`) materializes the entire request before fetch. No max-body
   cap anywhere. Additional wrinkle: the SSE/streaming branch is gated on
   `Accept: text/event-stream` (`service_dispatcher.py:246`), but browser
   `fetch()` typically sends `Accept: */*`, so anything streaming via NDJSON or
   chunked transfer falls into the buffering branch by default. Use
   `request.stream()` with `httpx`'s streaming content API and add an
   explicit max-body byte limit.

2. **Watchdog file watcher loses bytes at JSONL/UTF-8 boundaries**
   (`session_watcher.py:362-380`). `byte_offset` is advanced by the byte
   count of the read, then the tail is decoded with
   `new_data.decode("utf-8", errors="replace")`. Two distinct corruption
   modes:
   - **Mid-line read**: a read can return half of the last JSONL record. The
     decode succeeds, but `json.loads` rejects the partial line — and the
     watcher has already moved `byte_offset` past those bytes, so on the
     next poll it reads only the *second half* of that line, also rejected.
     The event is lost forever.
   - **Mid-codepoint read**: a read can split a multi-byte UTF-8 sequence;
     `errors="replace"` substitutes U+FFFD, mangling the JSON, and again
     `byte_offset` has already advanced.

   Both must be fixed together: scan the new bytes back to the last `\n`,
   only advance `byte_offset` to that boundary, and keep the unprocessed
   suffix for the next read. Also handle `current_size < byte_offset`
   (truncation / rotation): today the conditional is `if current_size >
   state.byte_offset` (`session_watcher.py:362`), so a shrunk file is
   silently ignored until it grows back past the stale offset — the watcher
   appears alive while serving stale data.

3. **SSE generator runs in a sync `StreamingResponse` and pins a threadpool worker per
   subscriber** (`server.py:299-337, 369-409`). `queue.get(timeout=1)` inside a `def`
   generator means N concurrent SSE clients permanently hold N threadpool workers
   (default ~40), starving every other `run_in_threadpool` call (screen capture,
   destroy, layout save, the WS broadcast loop). Convert to `async def` polling an
   `asyncio.Queue`.

4. **Unbounded in-memory growth across the server and client.** Every cache in
   the pipeline is append-only:
   - `_existing_event_ids` (set of UUID-derived strings, one per event) and
     `_tool_name_by_call_id` (map per tool call) in `session_watcher.py:74-75`
     — both grow forever, used only for dedup and enrichment.
   - `_session_states`, `_known_session_ids`, `_main_session_ids`,
     `_subagent_metadata` (`session_watcher.py:71-76`) — one entry per
     discovered session, never evicted when the agent is destroyed.
     `_known_session_ids` (line 72) is appended to but *never read* — dead
     storage that still consumes memory.
   - `event_queue: queue.Queue[...]` with no maxsize (`event_queues.py:34`)
     and `_event_buffers[agent_id]` (line 63) retain every `STORE` event
     ever broadcast. A disconnected SSE client plus a chatty agent = leak
     proportional to elapsed time.
   - Frontend `eventsByAgent` and `notFoundAgentIds` (`Response.ts:71-72`)
     never drop entries when an agent is destroyed. Switching between many
     agents over a long session accumulates every transcript ever loaded.
   - Frontend `logLines: string[]` in `ChatPanel.ts:81` grows for the
     duration of an agent's creation log.

   None of this matters for a five-minute demo. All of it matters for a
   workspace that stays open for a day.

5. **Side effects fired from inside Mithril `view()`**
   (`ChatPanel.ts:165-168, 314-318, 365`; `DockviewWorkspace.ts:962-972`).
   Concretely, every call to `renderMessages` (which is called from `view`)
   does:
   - `renderBuildLog` → `connectLogWs(agentId)` when `logAgentId !== agentId`
     (line 166-168) — *opens a WebSocket from a render function*. The
     `logAgentId` guard is the only thing stopping a fresh WS on every
     redraw.
   - `ensureAgentLoaded(agentId)` (line 314) — kicks off an async
     `fetchEvents` if the agent changed.
   - `manageStreamConnection(agentId)` (line 315) — opens an `EventSource`.
   - `fetchScreenCapture(agentId)` (line 318) — fires HTTP request, then
     calls `m.redraw()` in `finally` (line 107).
   - `startBackfill(agentId)` (line 365) — starts a long-running async loop
     that itself calls `m.redraw()` (line 239).

   Mithril views are required to be pure; `m.redraw()` called from a code
   path rooted in `view()` is documented as undefined behavior. Today it
   only works because the `await` defers the redraw to a microtask. Any
   refactor that removes one of the guards (e.g. someone moves backfill out
   of the panel) will reintroduce duplicate connections, infinite redraws,
   or lost state. Move every side effect to `oninit` / `onupdate` (with
   explicit prev-attrs comparison) / `onremove`.

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

## 6. Scaling to an arbitrarily long transcript

Think of a single agent whose session JSONL grows without bound — many days of
continuous use, hundreds of thousands of events, gigabytes on disk. The UI must
remain responsive throughout. Today, **every layer of the pipeline scales linearly
(or worse) with total session length**. There is no cursor-based read path, no
on-disk index, no eviction, no virtualization. The system is built around the
implicit assumption that "the whole transcript fits in memory and on screen."

### 6.1 Walk the data path

1. **JSONL file on disk** (`~/.claude/projects/.../<session>.jsonl`) grows append-only.
   Claude Code itself never rotates or compacts it — Claude's in-process context
   compaction does not produce a shorter on-disk session, it only changes what the
   model sees on subsequent turns.

2. **`AgentSessionWatcher._poll_for_changes`** (`session_watcher.py:362-389`) reads
   the *tail* incrementally via `byte_offset`. This part is O(delta), which is
   correct. New events are added to `_existing_event_ids` (set of UUIDs, +1 entry
   per event forever) and `_tool_name_by_call_id` (map call-id → tool name, +1 per
   tool call forever).

3. **`AgentSessionWatcher.get_all_events`** (`session_watcher.py:96-140`) reads
   *the whole file* via `state.file_path.read_text().splitlines()` and re-parses
   every line through `parse_session_lines`. This is the function that backs
   `_get_events` (`server.py:274-296`) — the initial-load endpoint — **and**
   `get_backfill_events` (line 142-158), which calls `get_all_events` and then
   slices. So every backfill page re-reads and re-parses the whole file.

4. **`_get_events` endpoint with no `before` param** (`server.py:290-294`) returns
   *all events as a single JSON response*. There is a hidden `_DEFAULT_TAIL_COUNT`
   default in `limit_str` but the no-`before` branch ignores it — `get_all_events`
   returns the entire list. For an N-event session, the response is O(N) bytes.

5. **`fetchEvents` on the client** (`Response.ts:117-135`) does
   `eventsByAgent[agentId] = result.events` — *replaces* the array wholesale, no
   trimming. For a long session the client now holds the full transcript in memory.

6. **`ChatPanel.renderMessages`** (`ChatPanel.ts:355-392`) builds a `toolResults`
   Map by iterating *every event*, then iterates again to build `messageNodes`.
   Both passes are O(N) per redraw. Mithril must then diff N vnodes against the DOM.
   There is no virtualization — every message is a real DOM subtree.

7. **`MarkdownContent`** (`markdown.ts:77-93`) renders one Markdown subtree per
   assistant message. `oncreate` runs `marked.parse()` + `DOMPurify.sanitize()`
   once per message at mount. `StableAssistantMessage.onbeforeupdate`
   (`message-renderers.ts:112-117`) memoizes correctly on event_id +
   tool_result count, so steady-state redraws don't re-parse — that's good. But
   the *first paint* of an N-event transcript runs N markdown parses
   synchronously.

8. **`runBackfillLoop`** (`ChatPanel.ts:230-257`) keeps paginating older events
   into the *same* in-memory array (`prependEvents`, `Response.ts:105-115`). The
   loop only stops when the server returns an empty page. For an infinite
   transcript the loop never terminates, and the array keeps growing on the
   bottom side as SSE delivers new events.

### 6.2 First thing that breaks

Run the numbers for a hypothetical session with 100k events, ~2 KB per event:

| Concern | Scale point | Time to break |
| --- | --- | --- |
| **Server `read_text()` of session file** | 200 MB read + UTF-8 decode + `splitlines()` per `_get_events` and per backfill page | Tens of seconds *per request*; Python event loop blocked since `_get_events` is sync running in the threadpool, but each call still pins a thread for that duration. |
| **Server JSON response** | ~200 MB serialized as one `JSONResponse` | OOM on `JSONResponse(content={"events": events})` which builds a Python string in memory before sending. Browsers also choke on multi-hundred-MB JSON parses. |
| **Client `eventsByAgent[agentId]`** | 100k × event objects | ~hundreds of MB heap; tab slows, GC pressure, eventual crash. |
| **DOM nodes** | ~100k message blocks, each with markdown subtree | Browsers degrade past ~10k nodes — scrolling lags, layout passes take seconds, devtools become unusable. |
| **Mithril vnode diff** | O(N) per redraw, ~5+ redraws per second under live streaming | Main thread saturated; UI drops below 30 fps. |
| **Initial markdown parse** | 100k × `marked.parse` + DOMPurify | Multi-minute white-screen on first load. |
| **Event-queue buffer** | `_event_buffers[agent_id]` retains every STORE event (`event_queues.py:63`) | Memory grows on the server too, even if no one is subscribed. |

The **first** thing to actually fail is item 2: `_get_events` returning every
event as a single JSON response. The server happily builds the response, the
HTTP layer happily sends it, and the browser dies decoding it. After that, every
other failure mode follows in order.

### 6.3 What the architecture is missing

To support an unbounded transcript, the pipeline needs:

1. **A bounded initial-load contract.** `GET /api/agents/:id/events` with no
   `before` should return the most recent N events (e.g. 200), not all of them.
   The `_DEFAULT_TAIL_COUNT` constant already exists but isn't actually applied to
   the no-`before` branch. Add `tail=N` semantics to `get_all_events`.

2. **A cursor-based, indexed event store.** Rebuilding the event list from raw
   JSONL on every page is the root cause of most of the server-side scaling
   problems. Two pragmatic options:
   - Keep an in-memory **append-only deque** of parsed events per session, plus a
     `dict[event_id, index]` for cursor lookups. Cap it at the most recent K
     events; serve older pages by seeking into the JSONL file using a separate
     on-disk index (one entry per event: `(event_id, byte_offset)`).
   - Or persist parsed events to SQLite (one DB per session, indexed by sequence
     and event_id). Disk-cheap, supports range queries, survives restart.

3. **A byte-offset index alongside the JSONL.** Maintain a sidecar file
   (`<session>.idx`) with `event_id → byte_offset`. Backfill becomes
   `seek(offset); read(N lines)` instead of "read 200 MB and slice."

4. **Streaming response for backfill pages.** Even a 50-event page can be large
   (long tool outputs). Use `StreamingResponse` with a JSON-lines body so the
   client doesn't wait for the entire page to assemble server-side.

5. **Frontend list virtualization.** Render only the messages currently in the
   viewport (plus a small over-scan). Libraries exist (`@tanstack/virtual`,
   `mithril-infinite`) — or roll a simple windowed list using `IntersectionObserver`.
   Combined with an `Intl.Segmenter`-style "approximate row height" estimator,
   this caps the DOM cost at O(visible) regardless of transcript length.

6. **Client-side eviction.** When the user scrolls far enough from a section, drop
   those events from `eventsByAgent` and re-fetch on scroll-back. Today the
   client treats `eventsByAgent` as an infinite cache.

7. **Pre-rendered markdown cache.** Memoize `renderMarkdown(content)` keyed by
   event_id (markdown text is immutable per event). Today `MarkdownContent.oncreate`
   runs DOMPurify + marked unconditionally; first paint of a long transcript is
   the dominant cost.

8. **Bounded SSE buffer with overflow signaling.** `_event_buffers` and
   `event_queue` (`event_queues.py:34, 63`) need a max size; on overflow either
   drop oldest with an explicit "you missed events, refetch from cursor X"
   message, or apply backpressure to the producer. A slow SSE consumer
   currently leaks memory linearly.

9. **Compaction summaries as first-class events.** Long-running agents compact
   their internal context periodically. The session file should be allowed to
   contain "summary" events that stand in for the elided turns; the UI would
   render these as collapsed-by-default cards. This is the only way to *also*
   keep the conceptual model usable for the human reader of a 100k-event
   transcript.

10. **Watcher-level backpressure on giant initial reads.** When a session file is
    discovered for the first time and is already 500 MB, the watcher should *not*
    eagerly read+parse the entire thing on the first `get_all_events` call. It
    should read the tail incrementally and serve "older" pages on demand from the
    sidecar index.

### 6.4 Quick wins (no architecture change)

Even before the bigger restructure, these are cheap fixes that materially
improve the responsiveness ceiling:

- **`_get_events` with no `before`: apply a tail limit.** One-line fix in
  `server.py:290-294`; cap the initial payload to the most recent N events. The
  client already supports backfill, so older events stream in naturally.
- **`get_all_events`: cache the parsed event list and invalidate on file
  growth.** Today every call re-reads the whole file. A simple cache keyed by
  `(file_path, file_size)` would make repeat backfill pages O(1) plus the slice.
- **Bound `_event_buffers[agent_id]` to the most recent K events** and on
  overflow surface a "client should refetch" sentinel.
- **Move `MarkdownContent.oncreate` parse off the critical path** using
  `requestIdleCallback` (text content can show as plain pre-wrap until parsed).
- **Add a key check in `MarkdownContent.onupdate`** so re-parses only happen
  when `vnode.attrs.content` differs from the last render — defends against
  parent re-renders that don't actually change content.
- **`ChatPanel.renderMessages`: memoize the `toolResults` Map** against
  `events` identity. Today it's rebuilt fresh every redraw (also true in
  `SubagentView.ts:140-145`).

### 6.5 Adjacent infinite-growth surfaces

Three other places have the same shape and same fixes:

- **Build log (`ChatPanel.ts:139, 187`).** `logLines.push(...)` appends forever;
  every line becomes a `<div>` keyed by index. For a long-running agent
  creation that emits megabytes of output, this is unbounded DOM + array.
- **Subagent transcripts (`SubagentView`).** Same `eventsByAgent`-style cache
  shape with no eviction.
- **Append-only request-writer (`request_writer.py:43`).** Refresh-request
  events file never rotates; one entry per refresh forever.

---

## 7. Suggested fix order

1. **Cap `_get_events` payload** when no `before` is provided (one-line fix,
   biggest single responsiveness win). [#6.4]
2. **Streaming bodies + max-body guard on the proxy** (#1) — biggest production
   risk for non-chat traffic.
3. **Watcher correctness**: JSONL boundary + rotation + cross-thread lock (#2
   plus `session_watcher.py:71`).
4. **Cache parsed event list in `get_all_events`** (memoize by file size) — kills
   backfill re-parse cost without an architectural change. [#6.4]
5. **Convert SSE generators to async** (#3); collapse the duplicate
   `_stream_events` / `_stream_subagent_events`.
6. **WS proxy `wait(FIRST_COMPLETED)`** + text/bytes detection fix (#6).
7. **Bound the event queue / per-agent buffers** and add eviction on agent
   destroy (#4).
8. **Move side effects out of `view()`** and wire `onremove` cleanly (#5).
9. **Reconnect backoff** (`StreamingMessage.ts:54-59`, `AgentManager.ts:54,
   101-104`).
10. **Frontend list virtualization** for `ChatPanel` and `SubagentView`. Required
    before any single transcript exceeds ~5k events.
11. **Sidecar event index + cursor-based backfill** — enables truly unbounded
    transcripts without server-side O(N) per request.
12. **Sweep silent excepts; add logging.**
13. **Delete shim/dead code** in `Response.ts`, `StreamingMessage.ts`,
    `Conversation.ts`, `ProtoAgentLogView.ts`.
