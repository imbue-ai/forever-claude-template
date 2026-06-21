Rewrote the system interface to be fully synchronous: replaced FastAPI/uvicorn with Flask served by the threaded Werkzeug server, and replaced all WebSocket handling with `flask-sock` (`simple-websocket` for the service proxy's backend leg). No `asyncio` remains anywhere in the app.

The HTTP/SSE/WebSocket wire contract is unchanged, so the frontend needs no changes: REST responses keep their status codes and JSON shapes, SSE streams emit identical frames (including `: keepalive`), and the `/api/ws`, proto-agent-logs, and `/service/<name>/` WebSocket endpoints speak the same messages.

Notable internals:

- `app.state` is replaced by a single typed `SystemInterfaceState` context object built once in `create_application` and read via `get_state()`.

- App-wide objects are built eagerly in `create_application`; teardown is wired in `main()` via `atexit` plus SIGTERM/SIGINT handlers (replacing the FastAPI lifespan and its SIGINT-only handler).

- The WebSocket broadcaster's asyncio task-cancellation machinery is gone; a wedged client is freed by `flask-sock`'s `ping_interval` keepalive or by the broadcaster pushing a shutdown sentinel into its queue. The `/service/<name>/` WebSocket proxy bridges the two directions with one thread each.

- Thread-safety: the per-agent session-watcher registry and the latchkey catalog cache are now guarded, since handlers run truly concurrently across threads under the threaded server.

- The `endpoint` plugin hook now receives a Flask app.

- Tests run against Flask's test client for HTTP/SSE and a real `run_simple` listener on an ephemeral port for the WebSocket endpoints.

- The `/service/<name>/` WebSocket proxy now resolves the backend host itself and connects to the first reachable address (IPv4 or IPv6), instead of `simple_websocket`'s default of trying only the first `getaddrinfo` result. Without this, a backend that binds IPv4 `127.0.0.1` only (e.g. ttyd) was unreachable on hosts where `localhost` resolves to IPv6 `::1` first -- the terminal tab showed "press enter to reconnect".

- The shared `flask-sock` server now echoes back whatever WebSocket subprotocol the client offered. ttyd's browser client opens its socket with the `tty` subprotocol, and Chrome aborts the handshake (close 1006, "press enter to reconnect") if the server's 101 response omits it; the proxy previously selected no subprotocol, so the terminal never connected even after the address-fallback fix. The broadcaster `/api/ws` and proto-agent-logs streams offer no subprotocol and are unaffected.
