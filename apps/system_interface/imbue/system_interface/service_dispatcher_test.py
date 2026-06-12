"""Integration tests for /service/<name>/ forwarding inside the system_interface.

Spins up a small stub FastAPI app on an ephemeral port as the "backend"
service, registers it with the system_interface's AgentManager via a
controlled applications.toml, and exercises the proxy end-to-end.
"""

import socket
import threading
import time
from collections.abc import AsyncGenerator
from collections.abc import Generator
from typing import Any

import anyio
import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.responses import PlainTextResponse
from fastapi.responses import Response
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from starlette.websockets import WebSocket
from starlette.websockets import WebSocketDisconnect

from imbue.system_interface.agent_manager import AgentManager
from imbue.system_interface.config import Config
from imbue.system_interface.models import ApplicationEntry
from imbue.system_interface.primitives import ServiceName
from imbue.system_interface.server import create_application
from imbue.system_interface.service_dispatcher import _RequestBodyTooLargeError
from imbue.system_interface.service_dispatcher import _build_rewritten_html_response
from imbue.system_interface.service_dispatcher import _capped_request_stream
from imbue.system_interface.service_dispatcher import _request_has_body
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster


def _find_free_port() -> int:
    """Return an ephemeral TCP port that is currently free."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _UvicornThread(threading.Thread):
    """Run a uvicorn server in a background thread for test scoping."""

    def __init__(self, app: FastAPI, port: int) -> None:
        super().__init__(daemon=True)
        self._config = uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="error")
        self.server = uvicorn.Server(self._config)

    def run(self) -> None:
        self.server.run()

    def stop(self) -> None:
        self.server.should_exit = True


def _wait_for_port(port: int, timeout_seconds: float = 3.0) -> None:
    """Poll until a TCP port is accepting connections."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"Backend port {port} did not come up within {timeout_seconds}s")


def _build_stub_backend() -> FastAPI:
    """Build a tiny FastAPI app that exercises the proxy's HTML/cookie/SSE paths."""
    stub = FastAPI()

    @stub.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(
            '<html><head><title>stub</title></head><body><a href="/relative-link">rel</a></body></html>'
        )

    @stub.get("/plain")
    def plain() -> PlainTextResponse:
        return PlainTextResponse("hello")

    @stub.get("/setcookie")
    def setcookie() -> PlainTextResponse:
        response = PlainTextResponse("ok")
        response.headers["Set-Cookie"] = "sid=abc; Path=/"
        return response

    @stub.get("/json")
    def json_endpoint() -> JSONResponse:
        return JSONResponse({"ok": True})

    @stub.get("/echo-query")
    def echo_query(request: Request) -> JSONResponse:
        return JSONResponse({"query": request.url.query})

    @stub.get("/events")
    def sse_endpoint() -> StreamingResponse:
        async def gen() -> AsyncGenerator[bytes, None]:
            yield b"data: chunk-1\n\n"
            yield b"data: chunk-2\n\n"

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    @stub.post("/echo-size")
    async def echo_size(request: Request) -> JSONResponse:
        body = await request.body()
        return JSONResponse({"size": len(body)})

    @stub.get("/big-binary")
    def big_binary() -> StreamingResponse:
        total_chunks = 64
        chunk = b"x" * 65536

        async def gen() -> AsyncGenerator[bytes, None]:
            for _ in range(total_chunks):
                yield chunk

        return StreamingResponse(
            gen(),
            media_type="application/octet-stream",
            headers={"X-Total-Size": str(total_chunks * len(chunk))},
        )

    @stub.websocket("/ws-echo")
    async def ws_echo(websocket: WebSocket) -> None:
        await websocket.accept()
        connected = True
        try:
            while connected:
                msg = await websocket.receive_text()
                await websocket.send_text(f"echo:{msg}")
        except WebSocketDisconnect:
            connected = False

    @stub.websocket("/ws-server-close")
    async def ws_server_close(websocket: WebSocket) -> None:
        # Accept then immediately close from the backend side, without the
        # client ever sending anything. Exercises the proxy path where the
        # backend->client direction finishes first while client->backend is
        # still parked on receive().
        await websocket.accept()
        await websocket.close()

    return stub


@pytest.fixture
def stub_backend() -> Generator[tuple[str, int], None, None]:
    """Start the stub backend and yield (base_url, port)."""
    port = _find_free_port()
    thread = _UvicornThread(_build_stub_backend(), port)
    thread.start()
    try:
        _wait_for_port(port)
        yield f"http://127.0.0.1:{port}", port
    finally:
        thread.stop()
        thread.join(timeout=2)


@pytest.fixture
def workspace_app_with_stub(stub_backend: tuple[str, int], monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """Build a system_interface FastAPI app wired to a stub backend under service 'web'.

    Injects a pre-built ``AgentManager`` seeded with the stub's URL as the
    'web' service. The real ``mngr observe`` pipeline is not started, so the
    test doesn't need a live mngr host; service discovery is whatever we
    put in ``_applications``.
    """
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-test")

    broadcaster = WebSocketBroadcaster()
    agent_manager = AgentManager.build(broadcaster)
    agent_manager._applications = [ApplicationEntry(name="web", url=stub_backend[0])]

    return create_application(Config(), agent_manager=agent_manager)


@pytest.fixture
def workspace_client(workspace_app_with_stub: FastAPI) -> Generator[TestClient, None, None]:
    with TestClient(workspace_app_with_stub) as client:
        yield client


def test_service_sw_js_is_served_without_stub(workspace_client: TestClient) -> None:
    """The scoped service worker is served statically from the system_interface."""
    response = workspace_client.get("/service/web/__sw.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/javascript")
    assert "const PREFIX = '/service/web'" in response.text


def test_first_navigation_returns_bootstrap_when_sw_cookie_missing(workspace_client: TestClient) -> None:
    """First HTML navigation without the sw_installed cookie gets the bootstrap page."""
    response = workspace_client.get(
        "/service/web/",
        headers={"sec-fetch-mode": "navigate"},
    )
    assert response.status_code == 200
    assert "serviceWorker.register" in response.text


def test_forwarded_html_has_base_tag_and_ws_shim(workspace_client: TestClient) -> None:
    """Once the SW cookie is present, HTML responses from the backend get rewritten."""
    response = workspace_client.get(
        "/service/web/",
        headers={"sec-fetch-mode": "navigate"},
        cookies={"sw_installed_web": "1"},
    )
    assert response.status_code == 200
    assert '<base href="/service/web/">' in response.text
    assert "OrigWebSocket" in response.text


def test_forwarded_absolute_href_is_rewritten(workspace_client: TestClient) -> None:
    """Absolute-path attributes in HTML are rewritten to the service prefix."""
    response = workspace_client.get(
        "/service/web/",
        cookies={"sw_installed_web": "1"},
    )
    assert 'href="/service/web/relative-link"' in response.text


def test_forwarded_plain_text_is_unchanged(workspace_client: TestClient) -> None:
    """Non-HTML responses pass through as-is."""
    response = workspace_client.get(
        "/service/web/plain",
        cookies={"sw_installed_web": "1"},
    )
    assert response.status_code == 200
    assert response.text == "hello"


def test_forwarded_json_is_unchanged(workspace_client: TestClient) -> None:
    """JSON responses pass through as-is."""
    response = workspace_client.get(
        "/service/web/json",
        cookies={"sw_installed_web": "1"},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_set_cookie_is_rewritten_to_service_path(workspace_client: TestClient) -> None:
    """Set-Cookie headers are scoped under /service/<name>/ so services don't pollute the origin."""
    response = workspace_client.get(
        "/service/web/setcookie",
        cookies={"sw_installed_web": "1"},
    )
    assert response.status_code == 200
    set_cookie = response.headers.get("set-cookie", "")
    assert "Path=/service/web/" in set_cookie


def test_unknown_service_returns_loading_page_for_html(workspace_client: TestClient) -> None:
    """Unknown service with HTML accept gets the auto-retrying loading page."""
    response = workspace_client.get(
        "/service/nonexistent/",
        headers={"accept": "text/html"},
    )
    assert response.status_code == 200
    assert "Loading..." in response.text
    assert "location.reload" in response.text


def test_unknown_service_returns_502_for_non_html(workspace_client: TestClient) -> None:
    """Unknown service for a non-HTML request returns 502 immediately."""
    response = workspace_client.get(
        "/service/nonexistent/api",
        headers={"accept": "application/json"},
    )
    assert response.status_code == 502


def test_forwarded_query_string_reaches_backend(workspace_client: TestClient) -> None:
    """Query string on the incoming request is preserved in the backend URL."""
    response = workspace_client.get(
        "/service/web/echo-query?foo=bar&baz=qux",
        cookies={"sw_installed_web": "1"},
    )
    assert response.status_code == 200
    assert response.json()["query"] == "foo=bar&baz=qux"


def test_forwarded_sse_is_streamed(workspace_client: TestClient) -> None:
    """An SSE request (accept: text/event-stream) streams chunks back to the client."""
    response = workspace_client.get(
        "/service/web/events",
        headers={"accept": "text/event-stream"},
        cookies={"sw_installed_web": "1"},
    )
    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")
    body = response.text
    assert "chunk-1" in body
    assert "chunk-2" in body


def test_websocket_echo_forwards_bidirectionally(workspace_client: TestClient) -> None:
    """The WS dispatcher byte-forwards messages between client and backend service."""
    with workspace_client.websocket_connect("/service/web/ws-echo") as ws:
        ws.send_text("hello")
        assert ws.receive_text() == "echo:hello"
        ws.send_text("world")
        assert ws.receive_text() == "echo:world"


@pytest.mark.timeout(15)
def test_websocket_backend_close_propagates_to_client(workspace_client: TestClient) -> None:
    """When the backend WS closes first, the proxy must cancel the still-parked
    client->backend direction and close the client socket rather than hanging
    forever (issue E)."""
    with pytest.raises(WebSocketDisconnect):
        with workspace_client.websocket_connect("/service/web/ws-server-close") as ws:
            # The client never sends; without the survivor-cancel fix the proxy
            # would stay blocked on the client->backend receive() and this
            # receive would hang until the test timeout instead of disconnecting.
            ws.receive_text()


def test_websocket_unknown_service_closes_with_4004(workspace_client: TestClient) -> None:
    """A WS upgrade against an unregistered service gets closed with 4004."""
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with workspace_client.websocket_connect("/service/nonexistent/anything") as ws:
            ws.receive_text()
    assert excinfo.value.code == 4004


def test_request_body_is_streamed_to_backend(workspace_client: TestClient) -> None:
    """A POST body reaches the backend intact via streaming forwarding."""
    payload = b"z" * (3 * 1024 * 1024)
    response = workspace_client.post(
        "/service/web/echo-size",
        content=payload,
        cookies={"sw_installed_web": "1"},
    )
    assert response.status_code == 200
    assert response.json()["size"] == len(payload)


def test_large_non_html_response_is_streamed_intact(workspace_client: TestClient) -> None:
    """A large non-HTML response streams through without buffering and arrives intact."""
    response = workspace_client.get("/service/web/big-binary", cookies={"sw_installed_web": "1"})
    assert response.status_code == 200
    assert response.headers.get("content-type", "").startswith("application/octet-stream")
    expected_size = int(response.headers["x-total-size"])
    assert len(response.content) == expected_size


def test_sse_streams_without_event_stream_accept_header(workspace_client: TestClient) -> None:
    """SSE streams even when the client sends the default Accept: */* (not text/event-stream).

    Regression for the old detection that gated streaming on the Accept header:
    fetch()-based consumers usually send */*, so they fell into the buffering
    branch. Streaming is now decided by the backend content-type instead.
    """
    response = workspace_client.get(
        "/service/web/events",
        headers={"accept": "*/*"},
        cookies={"sw_installed_web": "1"},
    )
    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")
    assert "chunk-1" in response.text
    assert "chunk-2" in response.text


def test_oversize_html_response_is_rejected() -> None:
    """An HTML response larger than the rewrite cap returns 502 instead of OOMing.

    HTML must be buffered to rewrite (base tag / WS shim / path rewriting), so
    it is the one body the proxy holds in memory; the cap bounds that.
    """
    oversized = b"<html><head></head><body>" + (b"a" * 4096) + b"</body></html>"
    backend_response = httpx.Response(200, headers={"content-type": "text/html"}, content=oversized)

    async def _drive() -> Response:
        return await _build_rewritten_html_response(backend_response, ServiceName("web"), max_bytes=1024)

    response = anyio.run(_drive)
    assert response.status_code == 502


def test_html_under_cap_is_rewritten() -> None:
    """HTML within the cap is buffered and gets the base tag injected."""
    backend_response = httpx.Response(
        200,
        headers={"content-type": "text/html"},
        content=b"<html><head></head><body>hi</body></html>",
    )

    async def _drive() -> Response:
        return await _build_rewritten_html_response(backend_response, ServiceName("web"), max_bytes=10_000)

    response = anyio.run(_drive)
    assert response.status_code == 200
    assert b'<base href="/service/web/">' in bytes(response.body)


def _make_streaming_request(chunks: list[bytes]) -> Request:
    """Build a Starlette Request whose .stream() yields the given body chunks."""
    messages: list[dict[str, Any]] = [
        {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
        for index, chunk in enumerate(chunks)
    ]
    cursor = {"index": 0}

    async def receive() -> dict[str, Any]:
        message = messages[cursor["index"]]
        cursor["index"] += 1
        return message

    scope: dict[str, Any] = {"type": "http", "method": "POST", "headers": []}
    return Request(scope, receive)


def test_capped_request_stream_aborts_when_body_exceeds_limit() -> None:
    """The request-body stream raises once cumulative bytes exceed the cap."""
    request = _make_streaming_request([b"a" * 100, b"b" * 100])

    async def _drive() -> list[bytes]:
        return [chunk async for chunk in _capped_request_stream(request, max_bytes=150)]

    with pytest.raises(_RequestBodyTooLargeError):
        anyio.run(_drive)


def test_capped_request_stream_passes_body_under_limit() -> None:
    """A body within the cap is yielded in full, unchanged."""
    request = _make_streaming_request([b"a" * 100, b"b" * 50])

    async def _drive() -> list[bytes]:
        return [chunk async for chunk in _capped_request_stream(request, max_bytes=1000)]

    chunks = anyio.run(_drive)
    assert b"".join(chunks) == b"a" * 100 + b"b" * 50


def test_request_has_body_detects_content_length_and_chunked() -> None:
    """_request_has_body gates whether a content stream is forwarded at all."""
    assert _request_has_body(_request_with_headers({"content-length": "10"})) is True
    assert _request_has_body(_request_with_headers({"transfer-encoding": "chunked"})) is True
    assert _request_has_body(_request_with_headers({"content-length": "0"})) is False
    assert _request_has_body(_request_with_headers({})) is False


def _request_with_headers(headers: dict[str, str]) -> Request:
    raw_headers = [(key.encode(), value.encode()) for key, value in headers.items()]

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    scope: dict[str, Any] = {"type": "http", "method": "POST", "headers": raw_headers}
    return Request(scope, receive)
