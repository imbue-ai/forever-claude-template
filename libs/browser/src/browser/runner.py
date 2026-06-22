"""Live-browser web service: spawn headful Chromium, stream it, drive it with browser-use.

Reached through the system_interface proxy at ``/service/browser/``. Serves one
self-contained page (assets/index.html) that renders the streamed browser on the
left and a status box + chat on the right. The page talks back over two
WebSockets: ``/sessions/{id}/cast`` (screencast frames out, human input + tab
control in) and ``/sessions/{id}/chat`` (browser-use task in, agent steps out).

``ROOT_PATH`` is read so FastAPI emits prefix-aware URLs behind the proxy; the
page itself uses relative URLs so it works under ``/service/browser/`` and at
``/`` standalone.
"""

import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi import WebSocket
from fastapi import WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from loguru import logger
from playwright.async_api import Error as PlaywrightError

from browser.session import BrowserSessionManager
from browser.session import BrowserStartupError
from browser.session import anthropic_key_status
from browser.session import deferred_install_ready

ROOT_PATH = os.environ.get("ROOT_PATH", "")
_INDEX_HTML = Path(__file__).parent / "assets" / "index.html"

app = FastAPI(title="browser", root_path=ROOT_PATH)
manager = BrowserSessionManager()


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


@app.post("/sessions")
async def create_session() -> JSONResponse:
    ready, reason = deferred_install_ready()
    if not ready:
        return JSONResponse({"error": reason}, status_code=503)
    available, _ = anthropic_key_status()
    try:
        session = await manager.create()
    except (BrowserStartupError, PlaywrightError, RuntimeError, OSError, ConnectionError) as e:
        logger.error("failed to create browser session: {}", e)
        return JSONResponse({"error": f"Could not start browser: {e}"}, status_code=503)
    return JSONResponse({"id": session.session_id, "key_available": available})


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str) -> JSONResponse:
    await manager.close(session_id)
    return JSONResponse({"closed": True})


@app.websocket("/sessions/{session_id}/cast")
async def cast_socket(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()
    try:
        session = manager.get(session_id)
    except KeyError:
        await websocket.close(code=1008)
        return
    session.add_cast_socket(websocket)
    await session.send_initial_state(websocket)
    try:
        async for message in websocket.iter_json():
            await session.handle_cast_message(message)
    except WebSocketDisconnect:
        pass
    finally:
        session.remove_cast_socket(websocket)


@app.websocket("/sessions/{session_id}/chat")
async def chat_socket(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()
    try:
        session = manager.get(session_id)
    except KeyError:
        await websocket.close(code=1008)
        return
    session.add_chat_socket(websocket)
    try:
        async for message in websocket.iter_json():
            prompt = message.get("prompt")
            action = message.get("action")
            if prompt:
                await session.submit(prompt)
            elif action == "stop":
                await session.take_control()
            elif action == "cancel_queue":
                await session.cancel_queue()
    except WebSocketDisconnect:
        pass
    finally:
        session.remove_chat_socket(websocket)


@app.on_event("shutdown")
async def _shutdown() -> None:
    logger.info("browser service shutting down; closing sessions")
    await manager.shutdown()


def main() -> None:
    uvicorn.run(app, host="127.0.0.1", port=8081)


if __name__ == "__main__":
    main()
