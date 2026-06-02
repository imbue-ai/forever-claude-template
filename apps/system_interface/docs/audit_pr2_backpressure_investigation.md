# Investigation Report: PR 2 -- Backpressure & Body Streaming

Investigation of the four audit issues for PR 2. Implementation is deferred
until the plan below is confirmed. Line numbers refer to the current tree.

## Issue A -- Unbounded event queue and per-agent buffers

**Verdict: Partially confirmed.**

- `event_queues.py:34` -- `queue.Queue()` has no `maxsize`. `broadcast()`
  (`event_queues.py:56-68`) fans out via `put_nowait` to every registered
  queue. A disconnected SSE consumer is cleaned up within ~8s (Starlette
  closes the generator -> `GeneratorExit` -> `unregister`), but a merely
  slow consumer keeps draining slower than it fills -- unbounded growth.
  Confirmed leak.
- `event_queues.py:60-63` -- `_event_buffers[agent_id]` appends on every
  `STORE` event. Currently dormant: the only live caller, `server.py:154`,
  always passes `BufferBehavior.IGNORE`. `STORE` is the default in
  `broadcast()` (`event_queues.py:57`), so any future caller -- or a plugin
  wired through `register_event_broadcaster` (`server.py:102`) -- that omits
  `buffer_behavior` would leak. Latent, not active.

Producer backpressure is not feasible: `broadcast()` runs synchronously on
the watcher thread and fans out to all subscribers. The "drop + refetch from
cursor" sentinel is more than needed: SSE events here are `IGNORE` (not
replayable) and the frontend already does a full snapshot refetch on
reconnect (`reconnectWithSnapshot`, `StreamingMessage.ts:64`).

Fix: mirror `WebSocketBroadcaster` (`ws_broadcaster.py:16-23, 95-134`) --
bounded `queue.Queue(maxsize=N)`, per-subscriber consecutive-`queue.Full`
counter, evict on overflow via the `None` sentinel.

## Issue B -- No lifecycle eviction on agent destroy

**Verdict: Confirmed (with one correction).**

- `_destroy_agent` (`server.py:670-717`) -> `agent_manager.remove_agent`
  (`agent_manager.py:245-255`) only pops `_agents` and stops the
  applications.toml watcher. The server-side
  `application.state.watchers[agent_id]` (`AgentSessionWatcher`) is never
  stopped; its thread and watchdog observer keep running. `event_queues` is
  never evicted for the agent.
- Correction: the watcher does not pin a CPU core -- `_run` waits on
  `_wake_event.wait(timeout=_POLL_INTERVAL_SECONDS)` (`session_watcher.py:203`).
  The `agent_manager.py:108` comment is misleading.
- Frontend: `eventsByAgent`, `notFoundAgentIds`, `backfillComplete`
  (`Response.ts:71-73`) and the `StreamingMessage.ts` maps are never pruned.
  Low impact, but correct to fix.

Fix: eviction belongs in the `_destroy_agent` route handler (which has
`request.app.state`), not in `remove_agent`. Add `AgentEventQueues.evict`.
Drive frontend eviction off the `agents_updated` WS message.

## Issue C -- Sync SSE generators pin threadpool workers

**Verdict: Confirmed.**

`_stream_events` (`server.py:299-337`) and `_stream_subagent_events`
(`server.py:369-409`) are sync `def` generators handed to
`StreamingResponse`. The inner `while not is_shutdown` loop means one
`next()` call can block across up to 8 consecutive `get(timeout=1)` calls
before yielding a keepalive -- for an idle stream the worker is held
continuously. N SSE clients consume N of the ~40 default threadpool workers.
The two generators are ~90% duplicated.

Fix: `asyncio.Queue` is risky (producer runs on the watcher thread, not
thread-safe). Match `_run_ws_broadcast_loop` (`server.py:572-622`): keep a
`threading.Queue`, make the handler `async def`, loop
`await run_in_threadpool(event_queue.get, timeout=1)`. Collapse the two
generators into one helper parametrized by an optional `session_id` filter.

## Issue D -- Proxy buffers full request and response bodies

**Verdict: Confirmed.**

- `_forward_http_request` (`service_dispatcher.py:84`) and
  `_forward_http_request_streaming` (`service_dispatcher.py:129`) both
  `await request.body()` -- the full request is buffered even on the
  streaming path.
- `_build_proxy_response` (`service_dispatcher.py:191, 195`) reads
  `backend_response.content` / `.text` -- full response buffered.
- No max-body cap anywhere. SW `proxy.py:91` `arrayBuffer()` buffers the
  full request in the browser.
- Streaming detection on `Accept: text/event-stream`
  (`service_dispatcher.py:246`) is weak -- `fetch()` defaults to
  `Accept: */*`.

Fix: response cannot be blanket-streamed -- `_build_proxy_response` must
buffer HTML to run `rewrite_proxied_html`. Always
`http_client.send(..., stream=True)`, inspect `content-type`; buffer+rewrite
HTML under the cap, stream everything else. Pass `request.stream()` as
httpx `content`, wrapped with a byte counter enforcing the cap (413 on
exceed). Confirm `duplex: 'half'` browser support before changing the SW.

## Implementation outcome

Deviations from the investigation, decided during implementation:

- **Issue A** uses drop-and-evict mirroring `WebSocketBroadcaster` (bounded
  queue + consecutive-full counter + `None` sentinel), not a cursor sentinel.
  The dormant `_event_buffers` STORE path is also capped.
- **Issue B** eviction is driven by an agent-removed listener on
  `AgentManager` (fires for REST destroy and observe-driven destroy /
  host-destroy alike); the lifespan registers a listener that stops the
  session watcher and evicts the event queues. `_destroy_agent` runs
  `remove_agent` off the event loop so the watcher join does not block it.
- **Issue C** uses a `threading.Queue` polled via
  `run_in_threadpool(get, timeout=1)` inside an `async def` generator (the
  `_run_ws_broadcast_loop` pattern), not `asyncio.Queue` -- the producer runs
  on the watcher thread. The two generators collapse into `_sse_event_stream`
  with an optional `session_id` filter.
- **Issue D**: streaming is now decided by backend `content-type` (HTML
  buffered+rewritten under a cap, everything else streamed), which also fixes
  the weak `Accept` detection. httpx 0.28 forces `Transfer-Encoding: chunked`
  for async-iterable content, so stale `content-length` / `transfer-encoding`
  request headers are stripped. The byte caps are injected as parameters
  (testable without monkeypatch). The **service worker was left unchanged**:
  streaming request bodies require `duplex: 'half'`, which only works in
  Chrome over HTTP/2+HTTPS and is unsupported in Firefox/Safari and over
  plain-HTTP local dev. The server-side stream+cap is the portable protection
  for the stated OOM target (the server); browser-side request buffering
  remains a documented, portability-constrained limitation.
