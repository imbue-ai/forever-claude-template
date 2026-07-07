"""Live-browser fleet web service: spawn headless Chromium, stream it, drive it with browser-use.

Reached through the system_interface proxy at ``/service/browser/``. Serves one
self-contained viewer page (assets/index.html) that renders a streamed browser
and an "Agent has control" overlay; the page talks back over one WebSocket,
``/browsers/{name}/cast`` (screencast frames out; human input, tab control, and
take/return-control in). Browsers are addressed by NAME (a random ~2-word english
name like ``alex-smith``), not a sequential int; there is no default browser.

Agents drive the fleet over HTTP (see the ``agentic-browser-fleet`` CLI):

* ``GET  /browsers``            -- list every browser, its owner, and its tabs.
* ``POST /browsers``            -- start a new browser (body ``{"name": ...}`` optional;
  returns ``{"name": ...}``). 400 invalid name, 409 duplicate name or fleet full.
* ``POST /browsers/{name}/task``  -- acquire-or-wait, run a browser-use task, stream
  the thinking/action trace as line-delimited JSON, release on completion.
* ``POST /browsers/{name}/hold``  -- acquire-or-wait and hold the browser until the
  request disconnects (the ``lock`` verb); release on disconnect.
* ``POST /browsers/{name}/release`` -- give a browser back (only its owner can).

For ``task`` and ``hold`` the request connection IS the lease: if it drops, the
run is cancelled and the browser is released.

ARCHITECTURE: this is a synchronous Flask + flask-sock service (thread-per-
connection, served by a threaded Werkzeug HTTP/1.1 server). browser_use,
Playwright (async), and the per-browser ownership state machine in session.py
are all async and run on ONE background asyncio event loop, quarantined behind a
single :class:`~browser.loop_bridge.AsyncLoopBridge`. Every route handler reaches
the async world only through ``bridge.run(coro)`` (blocking) or ``bridge.submit``
(fire-and-forget, returns the in-loop asyncio.Task). This mirrors the proven
Flask+WS pattern in apps/system_interface. ``ROOT_PATH`` is read for informational
parity but is no longer wired into URL generation: the viewer uses relative URLs,
so the ``/service/browser/`` proxy prefix needs no server-side awareness (the
FastAPI ``root_path`` it replaced only emitted prefix-aware URLs the page never
relied on).
"""

import json
import os
import queue
import signal
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from types import FrameType
from typing import Any

from flask import Flask, Response, jsonify, request
from flask_sock import Sock
from loguru import logger
from simple_websocket import ConnectionClosed

from browser.loop_bridge import AsyncLoopBridge, cancel_task
from browser.names import is_valid_browser_name
from browser.session import (
    BrowserSessionManager,
    BrowserStartupError,
    DuplicateBrowserNameError,
    FleetFullError,
    InvalidBrowserNameError,
    LiveBrowser,
    # PlaywrightError comes from the engine module (session.py owns all Playwright/
    # browser_use interaction); the sync web layer never imports playwright itself.
    PlaywrightError,
    anthropic_key_status,
    deferred_install_ready,
)
from browser.wsgi import make_threaded_server

ROOT_PATH = os.environ.get("ROOT_PATH", "")
_INDEX_HTML = Path(__file__).parent / "assets" / "index.html"

# Errors raised when Chromium can't be launched (install not finished, CDP failure).
_STARTUP_ERRORS = (BrowserStartupError, PlaywrightError, RuntimeError, OSError, ConnectionError)

# How long a state-changing route's bridge.run waits before giving up and (via the
# bridge) cancelling the orphaned coroutine. The acquire/hold/task streaming paths
# legitimately block until granted/disconnected and pass timeout=None instead.
_ROUTE_TIMEOUT = float(os.environ.get("BROWSER_ROUTE_TIMEOUT", "120"))

# Direct-control browser ACTIONS (navigate/click/input/.../tab) can legitimately run long
# on a heavy page -- a navigation to a slow site can easily exceed the 120s _ROUTE_TIMEOUT,
# and the old FastAPI path had NO server-side timeout at all (finding [9]). Cancelling such
# an action mid-flight would surface a spurious 500 for a request that was about to succeed,
# so direct actions get their own generous timeout. A timeout cancellation is still SAFE for
# the ownership state machine: run_action sets the lease (and clears the claim window) BEFORE
# the action and runs the action under _lock, so a cancellation only unwinds the in-flight
# action + the _lock frame -- the lease stays held and no ownership field is left half-written
# (control mutations are atomic under _control_lock, which the action body never holds). The
# backstop against a truly-wedged action is still the idle-lease sweep. Env-tunable; set to 0
# for no timeout (the action then runs to completion or until the agent's own client drops).
_DIRECT_ACTION_TIMEOUT_RAW = float(os.environ.get("BROWSER_DIRECT_ACTION_TIMEOUT", "600"))
_DIRECT_ACTION_TIMEOUT: float | None = _DIRECT_ACTION_TIMEOUT_RAW if _DIRECT_ACTION_TIMEOUT_RAW > 0 else None

# Outbound-drain / inbound-poll cadence for the cast handler and the NDJSON
# generators. The 0.5s NDJSON poll both flushes a heartbeat (so a dead client
# surfaces as a write failure in bounded time) and re-checks the run's state.
_NDJSON_POLL_SECONDS = 0.5
_CAST_OUTBOUND_POLL_SECONDS = 1.0
_CAST_INBOUND_POLL_SECONDS = 0.05

# The ONE sync<->async boundary: every route reaches the async world through this
# bridge's single background loop (see browser.loop_bridge). The manager and all
# LiveBrowsers are constructed/driven on that loop, so their asyncio locks/events
# keep their cooperative single-threaded meaning.
bridge = AsyncLoopBridge()
manager = BrowserSessionManager()

application = Flask(__name__, static_folder=None)
application.config["SOCK_SERVER_OPTIONS"] = {"ping_interval": 25}
sock = Sock(application)

# Init gate: cleared at import, set when startup restore finishes (always, even on
# failure -- see _startup). State-changing routes return 503 "initializing" until
# then; read-only routes (state/ls/health) stay open so the user can watch the
# fleet come back. A threading.Event (not asyncio.Event) because it is read on
# Flask threads and set from the loop's _startup finally -- both thread-safe.
_init_done = threading.Event()
# The startup status is written once per phase on the loop thread (``_startup``) and
# read on Flask route threads (``init_status``). A small lock guards both sides so a
# reader always sees one fully-consistent dict (never a torn phase/error combination),
# rather than relying on CPython name-rebind atomicity. The single-element holder lets
# the lock-guarded functions swap the value without the ``global`` keyword. See
# _publish_init_status / _read_init_status.
_init_status_lock = threading.Lock()
_init_status_holder: list[dict[str, Any]] = [{"phase": "initializing"}]


def _publish_init_status(status: dict[str, Any]) -> None:
    """Publish the startup status under the lock (called on the loop thread)."""
    with _init_status_lock:
        _init_status_holder[0] = status


def _read_init_status() -> dict[str, Any]:
    """Snapshot the startup status under the lock (called on a Flask reader thread)."""
    with _init_status_lock:
        return _init_status_holder[0]


def _error(payload: dict[str, Any], status: int) -> Response:
    """A JSON error Response with an explicit status code (Flask-idiomatic single value)."""
    response = jsonify(payload)
    response.status_code = status
    return response


def _require_ready() -> Response | None:
    """503 while the fleet is still restoring saved browsers; None once ready."""
    if not _init_done.is_set():
        return _error(
            {
                "error": "Browser fleet is still restoring your saved browsers; try again in a moment.",
                "status": "initializing",
            },
            503,
        )
    return None


async def _startup() -> None:
    """Restore the saved fleet (eager-sequential) behind the init gate. The gate is
    ALWAYS opened in ``finally`` so a restore failure can never wedge the daemon shut.

    Runs as a coroutine on the bridge loop (launched via ``bridge.submit`` at app
    construction), so it does not block read-only routes from serving immediately.
    """
    try:
        ready, reason = deferred_install_ready()
        if not ready:
            # Chromium isn't installed yet; don't block. The fleet starts empty and the
            # first create() (once the install marker appears) brings a browser up.
            _publish_init_status({"phase": "waiting_for_chromium", "reason": reason})
            return
        await manager.restore()
        _publish_init_status({"phase": "ready"})
    except _STARTUP_ERRORS as e:
        logger.error("browser fleet restore failed ({}); serving an empty fleet", e)
        _publish_init_status({"phase": "ready", "error": str(e)})
    finally:
        # Always run the periodic manifest checkpoint -- including the waiting-for-
        # chromium and restore-failed paths, where the fleet comes up lazily later;
        # otherwise tab-URL drift would never be persisted for the daemon's lifetime.
        manager.start_checkpointing()
        # threading.Event.set is thread-safe: this runs on the loop thread, readers
        # are Flask threads.
        _init_done.set()


def _ndjson(event: dict[str, Any]) -> str:
    return json.dumps(event, default=str) + "\n"


def _resolve_sync(browser_id: str) -> "LiveBrowser | Response":
    """Resolve a browser on the loop, turning KeyError into 404 / startup errors into 503."""
    try:
        return bridge.run(manager.resolve(browser_id), timeout=_ROUTE_TIMEOUT)
    except KeyError:
        return _error({"error": f"No browser {browser_id}"}, 404)
    except _STARTUP_ERRORS as e:
        return _error({"error": f"Could not start browser {browser_id}: {e}"}, 503)


def _agent_identity() -> tuple[str | None, str | None]:
    return request.headers.get("x-mngr-agent-id"), request.headers.get("x-mngr-agent-name")


def _body() -> dict[str, Any]:
    return request.get_json(silent=True) or {}


# --- read-only routes (no init gate) -----------------------------------------


def index() -> Response:
    return Response(_INDEX_HTML.read_text(), mimetype="text/html")


def health() -> Response:
    return jsonify({"status": "ok", "initializing": not _init_done.is_set()})


def init_status() -> Response:
    """Restore progress: phase is initializing / waiting_for_chromium / ready."""
    return jsonify(_read_init_status())


def key_status() -> Response:
    available, reason = anthropic_key_status()
    return jsonify({"available": available, "reason": reason})


def list_browsers() -> Response:
    """List the fleet (read-only; works during init). The fleet starts EMPTY -- there is
    no default browser, so nothing is materialized here.

    Also reports whether 'New browser' can run right now (``can_create`` + ``create_reason``
    + count/max) so the UI can gate its button -- mirroring what ``create_browser`` enforces.
    ``can_create`` is NOT gated on ``_init_done``: create works DURING restore (it queues
    behind the serialized relaunches), so the button must stay enabled during init. Only
    a missing Chromium install or the cap disables it."""
    available, _ = anthropic_key_status()
    ready, install_reason = deferred_install_ready()
    # capacity() reads the manager's _browsers dict, which is mutated on the loop
    # thread; reading it directly from this Flask worker thread can KeyError mid
    # iteration. Route it through the bridge so the read runs ON the loop thread,
    # like every other manager-state access here.
    count, cap = bridge.run(manager.capacity_async(), timeout=_ROUTE_TIMEOUT)
    if not ready:
        can_create, create_reason = False, install_reason or "installing browser support"
    elif count >= cap:
        can_create, create_reason = False, f"{count}/{cap} browsers open -- close one first"
    else:
        can_create, create_reason = True, ""
    return jsonify(
        {
            "browsers": bridge.run(manager.list_browsers(), timeout=_ROUTE_TIMEOUT),
            "key_available": available,
            "can_create": can_create,
            "create_reason": create_reason,
            "browser_count": count,
            "browser_max": cap,
        }
    )


# --- state-changing routes (init-gated) --------------------------------------


def create_browser() -> Response:
    """Register a new browser and return its name IMMEDIATELY (the Chromium launch runs
    in the background).

    NOT init-gated: create works DURING restore. ``manager.create`` registers the browser
    in ``init`` under ``manager._lock`` (cap check + name resolution + insert -- all fast,
    no Chromium launch) and returns at once, kicking the serialized launch off as a
    background task. So this route does NOT block on (or time out against) the multi-second
    launch -- the optimistic viewer pane finds the registered browser the instant it
    connects and watches it flip from ``init`` to ``running`` over the cast socket. The
    background launch persists the manifest itself once the browser is ``running``. The
    only hard pre-check is that Chromium is installed (else nothing to launch -> 503).

    Body ``{"name": "<name>"}`` is optional; omitted -> a random name is generated.
    Response ``{"name": <chosen-name>, "key_available": <bool>}``. Errors: 400 invalid
    name, 409 duplicate name or fleet full, 503 Chromium installing."""
    ready, reason = deferred_install_ready()
    if not ready:
        return _error({"error": reason}, 503)
    available, _ = anthropic_key_status()
    name = _body().get("name")
    try:
        # Returns fast: registers init + spawns the serialized launch on the loop.
        session = bridge.run(manager.create(name), timeout=_ROUTE_TIMEOUT)
    except InvalidBrowserNameError as e:
        return _error({"error": str(e)}, 400)
    except (DuplicateBrowserNameError, FleetFullError) as e:
        return _error({"error": str(e)}, 409)
    except _STARTUP_ERRORS as e:
        logger.error("failed to register browser: {}", e)
        return _error({"error": f"Could not start browser: {e}"}, 503)
    return jsonify({"name": session.browser_id, "key_available": available})


def close_browser(browser_id: str) -> Response:
    if (gate := _require_ready()) is not None:
        return gate
    bridge.run(manager.close(browser_id), timeout=_ROUTE_TIMEOUT)
    # Rewrite the manifest (name now gone) BEFORE deleting the profile, so a crash between
    # them leaves an orphan dir (swept next boot), never a manifest entry pointing at a
    # deleted profile. A manifest-write hiccup must not 500 the close or skip the
    # profile delete -- the periodic checkpoint will reconcile the manifest anyway.
    try:
        bridge.run(manager._save_manifest(), timeout=_ROUTE_TIMEOUT)
    except (OSError, *_STARTUP_ERRORS) as e:
        logger.warning("manifest save during close of browser {} failed ({})", browser_id, e)
    # Every browser is created on demand (no permanent default), so closing one always
    # forgets its persistent profile.
    manager.forget_profile_dir(browser_id)
    return jsonify({"closed": True})


def release_browser(browser_id: str) -> Response:
    if (gate := _require_ready()) is not None:
        return gate
    agent_id, _ = _agent_identity()
    if not agent_id:
        return _error({"error": "X-Mngr-Agent-Id header required"}, 400)
    resolved = _resolve_sync(browser_id)
    if isinstance(resolved, Response):
        return resolved
    return jsonify({"released": bridge.run(resolved.release(agent_id), timeout=_ROUTE_TIMEOUT)})


def _stream_acquire(
    gen_queue: "queue.Queue[dict[str, Any] | None]",
    acquire_task: Any,
    status_out: list[str],
) -> Iterator[str]:
    """Drain ``waiting`` events while a submitted ``acquire`` runs on the loop.

    ``acquire`` is submitted (returns the in-loop task immediately) so the Flask
    generator can stream the ``waiting`` line(s) its ``on_wait`` callback pushes
    onto ``gen_queue``. When the task finishes, its result is the final status; on
    a client disconnect mid-wait the generator's outer ``finally`` cancels the task
    (its existing CancelledError handler removes the waiter from ``_wait_queue`` on
    the loop) and records ``"disconnected"``.

    A parked acquire emits no events after the first ``waiting`` line, so without a
    heartbeat the WSGI server never writes again and a client that drops mid-wait is
    never noticed -- the waiter would hold its FIFO slot for the holder's whole lease
    (up to ~15 min) and block everyone behind it. So on each idle poll we yield a
    ``ping``: the forced socket write fails on a dead client, raising the
    ``GeneratorExit`` whose ``finally`` cancels the acquire (its CancelledError handler
    removes the waiter on the loop). This mirrors the run/hold loops' heartbeat -- the
    only disconnect signal available on a sync Flask/WSGI stream (there is no
    ``request.is_disconnected()``).
    """
    while not acquire_task.done():
        try:
            event = gen_queue.get(timeout=_NDJSON_POLL_SECONDS)
        except queue.Empty:
            # Heartbeat: force a write so a client that dropped while parked in the
            # wait queue surfaces as a broken-pipe GeneratorExit in bounded time.
            yield _ndjson({"type": "ping"})
            continue
        if event is not None:
            yield _ndjson(event)
    # Drain any events buffered after the task finished but before we noticed.
    yield from _drain_ndjson(gen_queue)
    # The acquire was submitted (fire-and-forget) so the wait-events could stream; now
    # block for its final status on the loop via the bridge (no web-layer coroutine needed).
    status_out.append(bridge.result(acquire_task))


def _drain_ndjson(gen_queue: "queue.Queue[dict[str, Any] | None]") -> Iterator[str]:
    """Yield every event currently buffered in ``gen_queue`` (until it is empty)."""
    drained = False
    while not drained:
        try:
            event = gen_queue.get_nowait()
        except queue.Empty:
            drained = True
            continue
        if event is not None:
            yield _ndjson(event)


def _make_on_wait(gen_queue: "queue.Queue[dict[str, Any] | None]") -> Callable[[str | None, str | None], Any]:
    async def on_wait(busy_id: str | None, busy_name: str | None) -> None:
        gen_queue.put_nowait({"type": "waiting", "busy_agent_id": busy_id, "busy_name": busy_name})

    return on_wait


def run_task(browser_id: str) -> Response:
    """Acquire-or-wait, run a browser-use task, and stream the trace as line-delimited JSON.

    The connection is the lease: a periodic heartbeat write surfaces a dead agent
    (Ctrl-C or container kill drops the socket) as a broken-pipe ``GeneratorExit``,
    whose ``finally`` cancels the run (via the in-loop task) and releases the
    browser. A human take-control also cancels the run, surfacing a ``preempted``
    event. The agent identity comes from the ``X-Mngr-Agent-*`` headers.
    """
    if (gate := _require_ready()) is not None:
        return gate
    agent_id, agent_name = _agent_identity()
    if not agent_id:
        return _error({"error": "X-Mngr-Agent-Id header required"}, 400)
    resolved = _resolve_sync(browser_id)
    if isinstance(resolved, Response):
        return resolved
    session = resolved
    body = _body()
    prompt = body.get("prompt")
    if not prompt:
        return _error({"error": "prompt is required"}, 400)
    reclaim = bool(body.get("reclaim", False))
    wait = bool(body.get("wait", True))
    max_wait = body.get("max_wait")

    def stream() -> Iterator[str]:
        gen_queue: "queue.Queue[dict[str, Any] | None]" = queue.Queue()
        status_out: list[str] = []
        acquire_task = bridge.submit(
            session.acquire(
                agent_id, agent_name, reclaim=reclaim, wait=wait, max_wait=max_wait,
                on_wait=_make_on_wait(gen_queue),
                # If a human has the browser pinned, acquire returns busy_human immediately
                # (the connection-bound wait queue is only for waiting on another AGENT).
                # Enrol the agent in the resume queue so it's messaged when the human hands
                # back -- otherwise a task/lock blocked by a human pin is silently dropped.
                enqueue_on_busy=True,
            )
        )
        try:
            yield from _stream_acquire(gen_queue, acquire_task, status_out)
        except GeneratorExit:
            # Client dropped during the acquire phase: cancel the acquire (its
            # CancelledError handler removes the waiter on the loop), then release.
            # release is a CAS no-op UNLESS a grant landed on the loop in the same poll
            # window the client dropped -- the wakeup beats the cancel, so acquire runs
            # to "acquired" and the cancel hits an already-done task. Without this that
            # just-granted lease is orphaned (no run task, dead connection) until the 90s
            # idle sweep, blocking everyone queued behind it. Mirrors the run finally.
            cancel_task(bridge.loop, acquire_task)
            bridge.run(session.release(agent_id), timeout=_ROUTE_TIMEOUT)
            raise
        status = status_out[0]
        if status != "acquired":
            if status != "disconnected":
                yield _ndjson({"type": status})
            return
        yield _ndjson({"type": "acquired", "browser_id": browser_id})

        async def emit(event: dict[str, Any]) -> None:
            gen_queue.put_nowait(event)

        run_task_handle = bridge.submit(session.run_agent(agent_id, prompt, emit))
        try:
            done = False
            while not done:
                try:
                    event = gen_queue.get(timeout=_NDJSON_POLL_SECONDS)
                except queue.Empty:
                    # Heartbeat write: forces a socket write so a dead client surfaces
                    # as a broken-pipe GeneratorExit in bounded time (no is_disconnected
                    # equivalent on Flask). Then re-check whether the run finished.
                    yield _ndjson({"type": "ping"})
                    done = run_task_handle.done()
                    continue
                if event is None:
                    continue
                yield _ndjson(event)
                # ``lost_control`` means a human took control (or the lease was swept)
                # between acquire and the run starting -- run_agent declined to drive and
                # returned, so end the stream just as for done/error.
                if event.get("type") in ("done", "error", "lost_control"):
                    done = True
            # Drain anything the run emitted right as it finished.
            yield from _drain_ndjson(gen_queue)
        finally:
            # Cancel the run on the loop (the existing run_agent finally CAS-no-ops the
            # release) and then release this agent's lease. Cancel covers both the
            # normal-finish path (a no-op: already done) and the disconnect path
            # (GeneratorExit), so a dropped client never leaves the agent driving a
            # "released" browser.
            cancel_task(bridge.loop, run_task_handle)
            bridge.run(session.release(agent_id), timeout=_ROUTE_TIMEOUT)

    return Response(stream(), mimetype="application/x-ndjson")


def hold_browser(browser_id: str) -> Response:
    """Acquire-or-wait and hold the browser until the request disconnects (the ``lock`` verb).

    Connection-bound, so a held lease always frees: when the holding client goes
    away (Ctrl-C / death) the heartbeat write fails, the generator's ``finally``
    runs, and the browser is released. No fire-and-forget lock exists.
    """
    if (gate := _require_ready()) is not None:
        return gate
    agent_id, agent_name = _agent_identity()
    if not agent_id:
        return _error({"error": "X-Mngr-Agent-Id header required"}, 400)
    resolved = _resolve_sync(browser_id)
    if isinstance(resolved, Response):
        return resolved
    session = resolved
    body = _body()
    reclaim = bool(body.get("reclaim", False))
    wait = bool(body.get("wait", True))
    max_wait = body.get("max_wait")

    def stream() -> Iterator[str]:
        gen_queue: "queue.Queue[dict[str, Any] | None]" = queue.Queue()
        status_out: list[str] = []
        acquire_task = bridge.submit(
            session.acquire(
                agent_id, agent_name, reclaim=reclaim, wait=wait, max_wait=max_wait,
                on_wait=_make_on_wait(gen_queue),
                # If a human has the browser pinned, acquire returns busy_human immediately
                # (the connection-bound wait queue is only for waiting on another AGENT).
                # Enrol the agent in the resume queue so it's messaged when the human hands
                # back -- otherwise a task/lock blocked by a human pin is silently dropped.
                enqueue_on_busy=True,
            )
        )
        try:
            yield from _stream_acquire(gen_queue, acquire_task, status_out)
        except GeneratorExit:
            # See run_task: release after cancel so a grant that landed in the drop
            # window isn't orphaned. CAS no-op when no grant landed.
            cancel_task(bridge.loop, acquire_task)
            bridge.run(session.release(agent_id), timeout=_ROUTE_TIMEOUT)
            raise
        status = status_out[0]
        if status != "acquired":
            if status != "disconnected":
                yield _ndjson({"type": status})
            return
        yield _ndjson({"type": "held", "browser_id": browser_id})
        try:
            held = True
            while held:
                # No agent run; just heartbeat-ping until the client drops. The
                # gen_queue is never written, so this always times out and pings --
                # the write is what makes a dead client surface as GeneratorExit.
                try:
                    gen_queue.get(timeout=_NDJSON_POLL_SECONDS)
                except queue.Empty:
                    yield _ndjson({"type": "ping"})
        finally:
            bridge.run(session.release(agent_id), timeout=_ROUTE_TIMEOUT)

    return Response(stream(), mimetype="application/x-ndjson")


# --- direct control: Claude drives the browser itself, one command at a time ---


def _direct_target(
    browser_id: str, gated: bool = True
) -> "tuple[LiveBrowser, str, str | None] | Response":
    """Resolve (browser, agent_id, agent_name) for a direct command, or an error Response.

    ``gated`` (default True) blocks the command with 503 "initializing" while the fleet
    is still restoring; read-only verbs (``state``) pass ``gated=False`` so the agent
    can look at whatever has already come back."""
    if gated and (gate := _require_ready()) is not None:
        return gate
    agent_id, agent_name = _agent_identity()
    if not agent_id:
        return _error({"error": "X-Mngr-Agent-Id header required"}, 400)
    resolved = _resolve_sync(browser_id)
    if isinstance(resolved, Response):
        return resolved
    return resolved, agent_id, agent_name


def cmd_acquire(browser_id: str) -> Response:
    """Explicitly reserve a browser across a run of commands (optional; the first
    command auto-acquires). ``--reclaim`` takes it back from a human who said 'keep going'."""
    target = _direct_target(browser_id)
    if isinstance(target, Response):
        return target
    session, agent_id, agent_name = target
    body = _body()
    # acquire AND read the control-state snapshot in ONE on-loop coroutine: the snapshot
    # reads loop-mutated ownership fields, so it must run on the loop (via the bridge),
    # not directly on this Flask thread (finding [4]).
    result = bridge.run(
        session.acquire_with_state(
            agent_id, agent_name,
            reclaim=bool(body.get("reclaim", False)),
            # `acquire` is the fast reserve-or-queue verb: it never blocks. A busy browser
            # enqueues the agent (woken when it frees) and returns immediately -- matching
            # what the CLI tells the agent ("you're queued ... messaged when it frees").
            # Blocking-wait lives in task/hold, which heartbeat and so detect a dropped
            # client; honoring wait=True on this non-streaming POST would pin a Flask
            # worker thread + a queue slot forever on a caller that walked away.
            wait=False,
            enqueue_on_busy=True,
        ),
        timeout=_ROUTE_TIMEOUT,
    )
    return jsonify(result)


def cmd_handoff(browser_id: str) -> Response:
    """Agent hands this browser to the human (e.g. a CAPTCHA it can't solve). The agent
    must currently hold it; it's put at the FRONT of the resume queue and control goes to
    the human, pinned, until they hand back -- then this agent resumes first."""
    target = _direct_target(browser_id)
    if isinstance(target, Response):
        return target
    session, agent_id, agent_name = target
    body = _body()
    reason = str(body.get("reason", "")).strip() or "human verification needed"
    # handoff AND its control-state snapshot in ONE on-loop coroutine (finding [4]): the
    # snapshot reads loop-mutated ownership fields, so it must not run on the Flask thread.
    result = bridge.run(session.handoff_with_state(agent_id, agent_name, reason), timeout=_ROUTE_TIMEOUT)
    return jsonify(result)


def cmd_state(browser_id: str) -> Response:
    # `state` is read-only -- allowed during init so the agent can look at the page
    # even before the whole fleet has finished restoring.
    target = _direct_target(browser_id, gated=False)
    if isinstance(target, Response):
        return target
    session, agent_id, agent_name = target
    # `state` does a CDP round-trip (get_state); use the generous direct-action timeout so a
    # heavy page isn't cancelled mid-read (finding [9]).
    return jsonify(bridge.run(session.act_state(agent_id, agent_name), timeout=_DIRECT_ACTION_TIMEOUT))


def cmd_navigate(browser_id: str) -> Response:
    target = _direct_target(browser_id)
    if isinstance(target, Response):
        return target
    session, agent_id, agent_name = target
    body = _body()
    url = body.get("url")
    if not url:
        return _error({"error": "url is required"}, 400)
    return jsonify(bridge.run(session.act_navigate(agent_id, agent_name, url), timeout=_DIRECT_ACTION_TIMEOUT))


def cmd_click(browser_id: str) -> Response:
    target = _direct_target(browser_id)
    if isinstance(target, Response):
        return target
    session, agent_id, agent_name = target
    body = _body()
    return jsonify(
        bridge.run(session.act_click(agent_id, agent_name, int(body.get("index", -1))), timeout=_DIRECT_ACTION_TIMEOUT)
    )


def cmd_input(browser_id: str) -> Response:
    target = _direct_target(browser_id)
    if isinstance(target, Response):
        return target
    session, agent_id, agent_name = target
    body = _body()
    return jsonify(
        bridge.run(
            session.act_input(agent_id, agent_name, int(body.get("index", -1)), str(body.get("text", ""))),
            timeout=_DIRECT_ACTION_TIMEOUT,
        )
    )


def cmd_select(browser_id: str) -> Response:
    target = _direct_target(browser_id)
    if isinstance(target, Response):
        return target
    session, agent_id, agent_name = target
    body = _body()
    return jsonify(
        bridge.run(
            session.act_select(agent_id, agent_name, int(body.get("index", -1)), str(body.get("value", ""))),
            timeout=_DIRECT_ACTION_TIMEOUT,
        )
    )


def cmd_scroll(browser_id: str) -> Response:
    target = _direct_target(browser_id)
    if isinstance(target, Response):
        return target
    session, agent_id, agent_name = target
    body = _body()
    return jsonify(
        bridge.run(
            session.act_scroll(agent_id, agent_name, str(body.get("direction", "down")), int(body.get("amount", 500))),
            timeout=_DIRECT_ACTION_TIMEOUT,
        )
    )


def cmd_keys(browser_id: str) -> Response:
    target = _direct_target(browser_id)
    if isinstance(target, Response):
        return target
    session, agent_id, agent_name = target
    body = _body()
    keys = body.get("keys")
    if not keys:
        return _error({"error": "keys is required"}, 400)
    return jsonify(bridge.run(session.act_keys(agent_id, agent_name, str(keys)), timeout=_DIRECT_ACTION_TIMEOUT))


def cmd_screenshot(browser_id: str) -> Response:
    target = _direct_target(browser_id)
    if isinstance(target, Response):
        return target
    session, agent_id, agent_name = target
    return jsonify(bridge.run(session.act_screenshot(agent_id, agent_name), timeout=_DIRECT_ACTION_TIMEOUT))


def cmd_tab(browser_id: str) -> Response:
    target = _direct_target(browser_id)
    if isinstance(target, Response):
        return target
    session, agent_id, agent_name = target
    body = _body()
    return jsonify(
        bridge.run(
            session.act_tab(agent_id, agent_name, str(body.get("action", "list")), body.get("index"), body.get("url")),
            timeout=_DIRECT_ACTION_TIMEOUT,
        )
    )


# --- screencast WebSocket ----------------------------------------------------


def _cast_inbound_pump(
    ws: Any, session: LiveBrowser, stop_event: threading.Event
) -> None:
    """Read inbound cast messages on a dedicated thread until the socket closes.

    Inbound (client->loop) and outbound (loop->client) are handled by two threads
    (this one reads; the handler's main thread drains the outbound queue and sends),
    so a slow inbound poll never stalls the outbound screencast and vice versa --
    the head-of-line blocking a single interleaved poll would cause. simple-websocket
    supports send and receive from different threads. Each inbound JSON message is
    dispatched to the loop via the bridge; commands are skipped while initializing
    (a human can't grab a half-restored fleet).
    """
    try:
        while not stop_event.is_set():
            data = ws.receive(timeout=_CAST_INBOUND_POLL_SECONDS)
            if data is None:
                continue  # poll timeout; re-check the stop flag and keep reading
            if not _init_done.is_set():
                continue  # the view streams read-only until the gate opens
            try:
                message = json.loads(data)
            except (ValueError, TypeError):
                continue
            kind = message.get("type")
            if kind == "take_control":
                bridge.run(session.take_control(), timeout=_ROUTE_TIMEOUT)
            elif kind == "return_to_agents":
                bridge.run(session.return_to_agents(), timeout=_ROUTE_TIMEOUT)
            else:
                bridge.run(session.handle_cast_message(message), timeout=_ROUTE_TIMEOUT)
    except ConnectionClosed:
        pass
    finally:
        stop_event.set()


def cast_socket(ws: Any, browser_id: str) -> None:
    """Bridge one cast WebSocket: outbound screencast frames + inbound input/control.

    Runs in its own Flask thread (thread-per-connection). The browser registers an
    outbound ``queue.Queue`` on the loop; ``LiveBrowser._broadcast`` (on the loop)
    pushes JSON frames onto it and this handler drains and sends them. A second
    thread reads inbound messages so neither direction blocks the other.
    """
    resolved = _resolve_sync_for_ws(browser_id)
    if resolved is None:
        # Three cases, distinguished by the close code so the viewer can react correctly:
        # - The name's background launch FAILED (finding [7]). A late/retrying optimistic
        #   viewer that was in 1013 backoff when it failed never registered a cast queue,
        #   so it missed the launch_failed broadcast and would otherwise retry forever.
        #   Close 1008 -- terminal, so the pane stops retrying and shows the failed state.
        # - The name is syntactically valid but no browser is registered under it YET.
        #   This is the OPTIMISTIC PANE opened on modal-accept BEFORE the serialized
        #   launch finished registering the name -- a transient miss, not "gone". Close
        #   1013 ("Try Again Later"); the viewer retries with backoff and connects once
        #   the launch registers the name.
        # - The name is invalid (could never exist). Close 1008 -- terminal, the viewer
        #   shows "browser closed -- reopen" and stops reconnecting.
        if bridge.run(manager.recently_failed_launch_async(browser_id), timeout=_ROUTE_TIMEOUT):
            ws.close(1008)  # launch failed -> terminal (stop retrying)
        elif is_valid_browser_name(browser_id):
            ws.close(1013)  # not yet created -> retryable
        else:
            ws.close(1008)  # gone / never valid -> terminal
        return
    session = resolved
    # Register + seed the initial control/tabs sync atomically on the loop, so no
    # live frame can interleave ahead of the state the viewer needs first. The lifecycle
    # is captured in the same on-loop step so the initializing banner below is consistent
    # with the seed.
    client_queue, lifecycle = bridge.run(session.register_cast_queue_with_lifecycle(), timeout=_ROUTE_TIMEOUT)
    if not _init_done.is_set() and lifecycle != "running":
        # The fleet is still restoring AND this browser isn't up yet: tell the viewer, so
        # it shows a banner and clears it on the first live frame/control once this browser
        # is up. A viewer joining an already-running browser is NOT told initializing
        # (finding [3-runner]) -- its seed already carries lifecycle=running and the live
        # page is streaming, so an initializing banner would be a false "still starting".
        # put_nowait is safe: the queue is fresh with at most a few seed messages and its
        # maxsize is far larger (finding [8]).
        client_queue.put_nowait(json.dumps({"type": "initializing"}))
    stop_event = threading.Event()
    inbound = threading.Thread(
        target=_cast_inbound_pump,
        kwargs={"ws": ws, "session": session, "stop_event": stop_event},
        name=f"browser-cast-inbound-{browser_id}",
        daemon=True,
    )
    inbound.start()
    try:
        while not stop_event.is_set():
            try:
                message = client_queue.get(timeout=_CAST_OUTBOUND_POLL_SECONDS)
            except queue.Empty:
                continue
            if message is None:
                break  # shutdown sentinel
            ws.send(message)
    except ConnectionClosed:
        pass
    finally:
        stop_event.set()
        inbound.join(timeout=5)
        bridge.run(session.unregister_cast_queue(client_queue), timeout=_ROUTE_TIMEOUT)


def _resolve_sync_for_ws(browser_id: str) -> "LiveBrowser | None":
    """Resolve a browser for the cast socket; None on any KeyError/startup error."""
    try:
        return bridge.run(manager.resolve(browser_id), timeout=_ROUTE_TIMEOUT)
    except (KeyError, *_STARTUP_ERRORS):
        return None


# --- app construction + lifecycle --------------------------------------------


def _register_routes() -> None:
    application.add_url_rule("/", view_func=index, methods=["GET"])
    application.add_url_rule("/health", view_func=health, methods=["GET"])
    application.add_url_rule("/init-status", view_func=init_status, methods=["GET"])
    application.add_url_rule("/key-status", view_func=key_status, methods=["GET"])
    application.add_url_rule("/browsers", view_func=list_browsers, methods=["GET"])
    application.add_url_rule("/browsers", view_func=create_browser, methods=["POST"], endpoint="create_browser")
    application.add_url_rule("/browsers/<string:browser_id>", view_func=close_browser, methods=["DELETE"])
    application.add_url_rule("/browsers/<string:browser_id>/release", view_func=release_browser, methods=["POST"])
    application.add_url_rule("/browsers/<string:browser_id>/task", view_func=run_task, methods=["POST"])
    application.add_url_rule("/browsers/<string:browser_id>/hold", view_func=hold_browser, methods=["POST"])
    application.add_url_rule("/browsers/<string:browser_id>/acquire", view_func=cmd_acquire, methods=["POST"])
    application.add_url_rule("/browsers/<string:browser_id>/handoff", view_func=cmd_handoff, methods=["POST"])
    application.add_url_rule("/browsers/<string:browser_id>/state", view_func=cmd_state, methods=["POST"])
    application.add_url_rule("/browsers/<string:browser_id>/navigate", view_func=cmd_navigate, methods=["POST"])
    application.add_url_rule("/browsers/<string:browser_id>/click", view_func=cmd_click, methods=["POST"])
    application.add_url_rule("/browsers/<string:browser_id>/input", view_func=cmd_input, methods=["POST"])
    application.add_url_rule("/browsers/<string:browser_id>/select", view_func=cmd_select, methods=["POST"])
    application.add_url_rule("/browsers/<string:browser_id>/scroll", view_func=cmd_scroll, methods=["POST"])
    application.add_url_rule("/browsers/<string:browser_id>/keys", view_func=cmd_keys, methods=["POST"])
    application.add_url_rule("/browsers/<string:browser_id>/screenshot", view_func=cmd_screenshot, methods=["POST"])
    application.add_url_rule("/browsers/<string:browser_id>/tab", view_func=cmd_tab, methods=["POST"])
    sock.route("/browsers/<string:browser_id>/cast")(cast_socket)


_register_routes()


def create_app() -> Flask:
    """Start the bridge loop and launch the (async) startup restore on it, then return
    the app. Idempotent-enough for the daemon entrypoint: ``main`` calls this once.

    Startup is async-on-the-loop (``bridge.submit``), not blocking app construction,
    so read-only routes serve immediately and return 503/initializing until the gate
    opens -- exactly as before.
    """
    bridge.start()
    bridge.submit(_startup())
    return application


def _shutdown() -> None:
    """Drain in-flight loop work, close the fleet, and stop the bridge loop.

    Owned exclusively by the signal handler (SIGTERM/SIGINT). ``manager.shutdown``
    cancels the checkpoint loop, writes a final manifest, and closes every browser
    (each browser's close stops its agent + screencast); then we stop the loop. We
    do NOT also register an atexit handler -- a single owner avoids double-closing
    the fleet or stopping an already-stopped loop.
    """
    logger.info("browser service shutting down; closing sessions")
    try:
        bridge.run(manager.shutdown(), timeout=_ROUTE_TIMEOUT)
    except (TimeoutError, *_STARTUP_ERRORS) as e:
        logger.warning("manager shutdown did not complete cleanly ({})", e)
    bridge.stop()


def _exit_on_signal(_signum: int, _frame: FrameType | None) -> None:
    raise SystemExit(0)


def main() -> None:
    """Build the app, register shutdown, and serve on the threaded HTTP/1.1 server.

    Replaces ``uvicorn.run``. The supervisord command line and ``ROOT_PATH`` env are
    unchanged; ``ROOT_PATH`` is now only informational (the viewer uses relative URLs,
    so the proxy prefix needs no server-side awareness).
    """
    app = create_app()
    signal.signal(signal.SIGTERM, _exit_on_signal)
    signal.signal(signal.SIGINT, _exit_on_signal)
    server = make_threaded_server("127.0.0.1", 8081, app)
    try:
        server.serve_forever()
    finally:
        _shutdown()


if __name__ == "__main__":
    main()
