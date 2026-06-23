"""Live-browser fleet web service: spawn headless Chromium, stream it, drive it with browser-use.

Reached through the system_interface proxy at ``/service/browser/``. Serves one
self-contained viewer page (assets/index.html) that renders a streamed browser
and an "Agent has control" overlay; the page talks back over one WebSocket,
``/browsers/{id}/cast`` (screencast frames out; human input, tab control, and
take/return-control in).

Agents drive the fleet over HTTP (see the ``agentic-browser-fleet`` CLI):

* ``GET  /browsers``            -- list every browser, its owner, and its tabs.
* ``POST /browsers``            -- start a new browser (409 when the fleet is full).
* ``POST /browsers/{id}/task``  -- acquire-or-wait, run a browser-use task, stream
  the thinking/action trace as line-delimited JSON, release on completion.
* ``POST /browsers/{id}/hold``  -- acquire-or-wait and hold the browser until the
  request disconnects (the ``lock`` verb); release on disconnect.
* ``POST /browsers/{id}/release`` -- give a browser back (only its owner can).

For ``task`` and ``hold`` the request connection IS the lease: if it drops, the
run is cancelled and the browser is released. ``ROOT_PATH`` is read so FastAPI
emits prefix-aware URLs behind the proxy; the page itself uses relative URLs so
it works under ``/service/browser/`` and at ``/`` standalone.
"""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi import Request
from fastapi import WebSocket
from fastapi import WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.responses import Response
from fastapi.responses import StreamingResponse
from loguru import logger
from playwright.async_api import Error as PlaywrightError

from browser.session import BrowserSessionManager
from browser.session import BrowserStartupError
from browser.session import FleetFullError
from browser.session import LiveBrowser
from browser.session import anthropic_key_status
from browser.session import deferred_install_ready

ROOT_PATH = os.environ.get("ROOT_PATH", "")
_INDEX_HTML = Path(__file__).parent / "assets" / "index.html"

# Errors raised when Chromium can't be launched (install not finished, CDP failure).
_STARTUP_ERRORS = (BrowserStartupError, PlaywrightError, RuntimeError, OSError, ConnectionError)

app = FastAPI(title="browser", root_path=ROOT_PATH)
manager = BrowserSessionManager()


def _ndjson(event: dict[str, Any]) -> str:
    return json.dumps(event, default=str) + "\n"


async def _next_event(queue: "asyncio.Queue[dict[str, Any]]", timeout: float) -> dict[str, Any] | None:
    try:
        return await asyncio.wait_for(queue.get(), timeout=timeout)
    except TimeoutError:
        return None


async def _suppress(task: "asyncio.Task[Any]") -> None:
    try:
        await task
    except (asyncio.CancelledError, *_STARTUP_ERRORS):
        pass


async def _resolve(browser_id: int) -> LiveBrowser:
    """Return the browser, lazily (re)creating the default browser 0 on demand.

    Browser 0 is the permanent default: ``ensure_browser_0`` is idempotent, so a
    first access (or an access after a daemon restart) brings it back. Any other id
    must already exist -- a closed id is gone (KeyError -> 404), never reused.
    """
    if browser_id == 0:
        return await manager.ensure_browser_0()
    return manager.get(browser_id)


async def _acquire_phase(
    session: LiveBrowser,
    agent_id: str,
    agent_name: str | None,
    *,
    reclaim: bool,
    wait: bool,
    max_wait: float | None,
    request: Request,
    queue: "asyncio.Queue[dict[str, Any]]",
    status_out: list[str],
) -> AsyncIterator[str]:
    """Acquire-or-wait, streaming any ``waiting`` status live, and record the outcome.

    Yields NDJSON lines (e.g. a ``waiting`` event while parked behind another agent)
    and appends the final status to ``status_out``: ``"acquired"``, ``"busy_human"``,
    ``"busy_agent"``, ``"timed_out"``, or ``"disconnected"`` (the client left while
    waiting). Acquire runs concurrently with the drain so the wait streams live.
    """

    async def on_wait(busy_id: str | None, busy_name: str | None) -> None:
        queue.put_nowait({"type": "waiting", "busy_agent_id": busy_id, "busy_name": busy_name})

    acquiring = asyncio.create_task(
        session.acquire(agent_id, agent_name, reclaim=reclaim, wait=wait, max_wait=max_wait, on_wait=on_wait)
    )
    while not acquiring.done():
        if await request.is_disconnected():
            acquiring.cancel()
            await _suppress(acquiring)
            status_out.append("disconnected")
            return
        event = await _next_event(queue, timeout=0.5)
        if event is not None:
            yield _ndjson(event)
    status_out.append(await acquiring)
    while not queue.empty():
        yield _ndjson(queue.get_nowait())


def _agent_identity(request: Request) -> tuple[str | None, str | None]:
    return request.headers.get("x-mngr-agent-id"), request.headers.get("x-mngr-agent-name")


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(_INDEX_HTML.read_text())


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/key-status")
def key_status() -> dict[str, object]:
    available, reason = anthropic_key_status()
    return {"available": available, "reason": reason}


@app.get("/browsers")
async def list_browsers() -> JSONResponse:
    """List the fleet. Best-effort ensures browser 0 exists so the default is always shown."""
    available, _ = anthropic_key_status()
    ready, _ = deferred_install_ready()
    if ready:
        try:
            await manager.ensure_browser_0()
        except _STARTUP_ERRORS as e:
            logger.debug("ensure_browser_0 during list ignored ({})", e)
    return JSONResponse({"browsers": await manager.list_browsers(), "key_available": available})


@app.post("/browsers")
async def create_browser() -> JSONResponse:
    ready, reason = deferred_install_ready()
    if not ready:
        return JSONResponse({"error": reason}, status_code=503)
    available, _ = anthropic_key_status()
    try:
        session = await manager.create()
    except FleetFullError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except _STARTUP_ERRORS as e:
        logger.error("failed to create browser: {}", e)
        return JSONResponse({"error": f"Could not start browser: {e}"}, status_code=503)
    return JSONResponse({"id": session.browser_id, "key_available": available})


@app.delete("/browsers/{browser_id}")
async def close_browser(browser_id: int) -> JSONResponse:
    await manager.close(browser_id)
    return JSONResponse({"closed": True})


@app.post("/browsers/{browser_id}/release")
async def release_browser(browser_id: int, request: Request) -> JSONResponse:
    agent_id, _ = _agent_identity(request)
    if not agent_id:
        return JSONResponse({"error": "X-Mngr-Agent-Id header required"}, status_code=400)
    try:
        session = await _resolve(browser_id)
    except KeyError:
        return JSONResponse({"error": f"No browser {browser_id}"}, status_code=404)
    return JSONResponse({"released": await session.release(agent_id)})


@app.post("/browsers/{browser_id}/task")
async def run_task(browser_id: int, request: Request) -> Response:
    """Acquire-or-wait, run a browser-use task, and stream the trace as line-delimited JSON.

    The connection is the lease: ``request.is_disconnected()`` is polled so a dead
    agent (Ctrl-C or container kill drops the socket) cancels the run and releases
    the browser. A human take-control cancels the run task too, surfacing a single
    ``preempted`` event. The agent identity comes from the ``X-Mngr-Agent-*`` headers.
    """
    agent_id, agent_name = _agent_identity(request)
    if not agent_id:
        return JSONResponse({"error": "X-Mngr-Agent-Id header required"}, status_code=400)
    try:
        session = await _resolve(browser_id)
    except KeyError:
        return JSONResponse({"error": f"No browser {browser_id}"}, status_code=404)
    except _STARTUP_ERRORS as e:
        return JSONResponse({"error": f"Could not start browser {browser_id}: {e}"}, status_code=503)
    body = await request.json()
    prompt = body.get("prompt")
    if not prompt:
        return JSONResponse({"error": "prompt is required"}, status_code=400)
    reclaim = bool(body.get("reclaim", False))
    wait = bool(body.get("wait", True))
    max_wait = body.get("max_wait")

    async def stream() -> AsyncIterator[str]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        status_out: list[str] = []
        async for line in _acquire_phase(
            session, agent_id, agent_name, reclaim=reclaim, wait=wait, max_wait=max_wait,
            request=request, queue=queue, status_out=status_out,
        ):
            yield line
        status = status_out[0]
        if status != "acquired":
            if status != "disconnected":
                yield _ndjson({"type": status})
            return
        yield _ndjson({"type": "acquired", "browser_id": browser_id})

        async def emit(event: dict[str, Any]) -> None:
            queue.put_nowait(event)

        run = asyncio.create_task(session.run_agent(prompt, emit))
        try:
            done = False
            while not done:
                if await request.is_disconnected():
                    run.cancel()
                    done = True
                    continue
                event = await _next_event(queue, timeout=0.5)
                if event is None:
                    done = run.done()
                    continue
                yield _ndjson(event)
                if event.get("type") in ("done", "error"):
                    done = True
            if not run.done():
                run.cancel()
            await _suppress(run)
            while not queue.empty():
                yield _ndjson(queue.get_nowait())
        finally:
            await session.release(agent_id)  # CAS: no-op if a human already took control

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@app.post("/browsers/{browser_id}/hold")
async def hold_browser(browser_id: int, request: Request) -> Response:
    """Acquire-or-wait and hold the browser until the request disconnects (the ``lock`` verb).

    Connection-bound, so a held lease always frees: when the holding client goes
    away (Ctrl-C / death) the browser is released. No fire-and-forget lock exists.
    """
    agent_id, agent_name = _agent_identity(request)
    if not agent_id:
        return JSONResponse({"error": "X-Mngr-Agent-Id header required"}, status_code=400)
    try:
        session = await _resolve(browser_id)
    except KeyError:
        return JSONResponse({"error": f"No browser {browser_id}"}, status_code=404)
    except _STARTUP_ERRORS as e:
        return JSONResponse({"error": f"Could not start browser {browser_id}: {e}"}, status_code=503)
    body = await request.json() if await request.body() else {}
    reclaim = bool(body.get("reclaim", False))
    wait = bool(body.get("wait", True))
    max_wait = body.get("max_wait")

    async def stream() -> AsyncIterator[str]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        status_out: list[str] = []
        async for line in _acquire_phase(
            session, agent_id, agent_name, reclaim=reclaim, wait=wait, max_wait=max_wait,
            request=request, queue=queue, status_out=status_out,
        ):
            yield line
        status = status_out[0]
        if status != "acquired":
            if status != "disconnected":
                yield _ndjson({"type": status})
            return
        yield _ndjson({"type": "held", "browser_id": browser_id})
        try:
            while not await request.is_disconnected():
                await asyncio.sleep(0.5)
        finally:
            await session.release(agent_id)

    return StreamingResponse(stream(), media_type="application/x-ndjson")


# --- direct control: Claude drives the browser itself, one command at a time ---


async def _direct_target(browser_id: int, request: Request) -> "tuple[LiveBrowser, str, str | None] | JSONResponse":
    """Resolve (browser, agent_id, agent_name) for a direct command, or an error response."""
    agent_id, agent_name = _agent_identity(request)
    if not agent_id:
        return JSONResponse({"error": "X-Mngr-Agent-Id header required"}, status_code=400)
    try:
        session = await _resolve(browser_id)
    except KeyError:
        return JSONResponse({"error": f"No browser {browser_id}"}, status_code=404)
    except _STARTUP_ERRORS as e:
        return JSONResponse({"error": f"Could not start browser {browser_id}: {e}"}, status_code=503)
    return session, agent_id, agent_name


async def _body(request: Request) -> dict[str, Any]:
    return await request.json() if await request.body() else {}


@app.post("/browsers/{browser_id}/acquire")
async def cmd_acquire(browser_id: int, request: Request) -> JSONResponse:
    """Explicitly reserve a browser across a run of commands (optional; the first
    command auto-acquires). ``--reclaim`` takes it back from a human who said 'keep going'."""
    target = await _direct_target(browser_id, request)
    if isinstance(target, JSONResponse):
        return target
    session, agent_id, agent_name = target
    body = await _body(request)
    status = await session.acquire(
        agent_id, agent_name,
        reclaim=bool(body.get("reclaim", False)),
        wait=bool(body.get("wait", False)),
        max_wait=body.get("max_wait"),
    )
    return JSONResponse({"ok": status == "acquired", "status": status, **session._control_state()})


@app.post("/browsers/{browser_id}/state")
async def cmd_state(browser_id: int, request: Request) -> JSONResponse:
    target = await _direct_target(browser_id, request)
    if isinstance(target, JSONResponse):
        return target
    session, agent_id, agent_name = target
    return JSONResponse(await session.act_state(agent_id, agent_name))


@app.post("/browsers/{browser_id}/navigate")
async def cmd_navigate(browser_id: int, request: Request) -> JSONResponse:
    target = await _direct_target(browser_id, request)
    if isinstance(target, JSONResponse):
        return target
    session, agent_id, agent_name = target
    body = await _body(request)
    url = body.get("url")
    if not url:
        return JSONResponse({"error": "url is required"}, status_code=400)
    return JSONResponse(await session.act_navigate(agent_id, agent_name, url))


@app.post("/browsers/{browser_id}/click")
async def cmd_click(browser_id: int, request: Request) -> JSONResponse:
    target = await _direct_target(browser_id, request)
    if isinstance(target, JSONResponse):
        return target
    session, agent_id, agent_name = target
    body = await _body(request)
    return JSONResponse(await session.act_click(agent_id, agent_name, int(body.get("index", -1))))


@app.post("/browsers/{browser_id}/input")
async def cmd_input(browser_id: int, request: Request) -> JSONResponse:
    target = await _direct_target(browser_id, request)
    if isinstance(target, JSONResponse):
        return target
    session, agent_id, agent_name = target
    body = await _body(request)
    return JSONResponse(await session.act_input(agent_id, agent_name, int(body.get("index", -1)), str(body.get("text", ""))))


@app.post("/browsers/{browser_id}/select")
async def cmd_select(browser_id: int, request: Request) -> JSONResponse:
    target = await _direct_target(browser_id, request)
    if isinstance(target, JSONResponse):
        return target
    session, agent_id, agent_name = target
    body = await _body(request)
    return JSONResponse(await session.act_select(agent_id, agent_name, int(body.get("index", -1)), str(body.get("value", ""))))


@app.post("/browsers/{browser_id}/scroll")
async def cmd_scroll(browser_id: int, request: Request) -> JSONResponse:
    target = await _direct_target(browser_id, request)
    if isinstance(target, JSONResponse):
        return target
    session, agent_id, agent_name = target
    body = await _body(request)
    return JSONResponse(await session.act_scroll(agent_id, agent_name, str(body.get("direction", "down")), int(body.get("amount", 500))))


@app.post("/browsers/{browser_id}/keys")
async def cmd_keys(browser_id: int, request: Request) -> JSONResponse:
    target = await _direct_target(browser_id, request)
    if isinstance(target, JSONResponse):
        return target
    session, agent_id, agent_name = target
    body = await _body(request)
    keys = body.get("keys")
    if not keys:
        return JSONResponse({"error": "keys is required"}, status_code=400)
    return JSONResponse(await session.act_keys(agent_id, agent_name, str(keys)))


@app.post("/browsers/{browser_id}/screenshot")
async def cmd_screenshot(browser_id: int, request: Request) -> JSONResponse:
    target = await _direct_target(browser_id, request)
    if isinstance(target, JSONResponse):
        return target
    session, agent_id, agent_name = target
    return JSONResponse(await session.act_screenshot(agent_id, agent_name))


@app.post("/browsers/{browser_id}/tab")
async def cmd_tab(browser_id: int, request: Request) -> JSONResponse:
    target = await _direct_target(browser_id, request)
    if isinstance(target, JSONResponse):
        return target
    session, agent_id, agent_name = target
    body = await _body(request)
    return JSONResponse(await session.act_tab(agent_id, agent_name, str(body.get("action", "list")), body.get("index"), body.get("url")))


@app.websocket("/browsers/{browser_id}/cast")
async def cast_socket(websocket: WebSocket, browser_id: int) -> None:
    await websocket.accept()
    try:
        session = await _resolve(browser_id)
    except (KeyError, *_STARTUP_ERRORS):
        await websocket.close(code=1008)  # viewer shows "browser closed -- reopen"
        return
    session.add_cast_socket(websocket)
    await session.send_initial_state(websocket)
    try:
        async for message in websocket.iter_json():
            kind = message.get("type")
            if kind == "take_control":
                await session.take_control()
            elif kind == "return_to_agents":
                await session.return_to_agents()
            else:
                await session.handle_cast_message(message)
    except WebSocketDisconnect:
        pass
    finally:
        session.remove_cast_socket(websocket)


@app.on_event("shutdown")
async def _shutdown() -> None:
    logger.info("browser service shutting down; closing sessions")
    await manager.shutdown()


def main() -> None:
    uvicorn.run(app, host="127.0.0.1", port=8081)


if __name__ == "__main__":
    main()
