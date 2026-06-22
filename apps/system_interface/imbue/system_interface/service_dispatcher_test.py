"""Integration tests for /service/<name>/ forwarding inside the system_interface.

Spins up a small stub Flask app on an ephemeral port as the "backend"
service, registers it with the system_interface's AgentManager via a
controlled applications.toml, and exercises the proxy end-to-end.
"""

from collections.abc import Iterator

import pytest
import simple_websocket
from flask import Flask
from flask import Response
from flask import request
from flask.testing import FlaskClient
from flask_sock import Sock

from imbue.system_interface.agent_manager import AgentManager
from imbue.system_interface.config import Config
from imbue.system_interface.models import ApplicationEntry
from imbue.system_interface.server import create_application
from imbue.system_interface.service_dispatcher import _connect_backend_websocket
from imbue.system_interface.testing import ServedApp
from imbue.system_interface.testing import close_ws
from imbue.system_interface.testing import open_ws
from imbue.system_interface.testing import serve_app
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster

_WS_RECEIVE_TIMEOUT = 10.0


def _build_stub_backend() -> Flask:
    """Build a tiny Flask app that exercises the proxy's HTML/cookie/SSE/WS paths."""
    stub = Flask(__name__, static_folder=None)
    sock = Sock(stub)

    @stub.route("/")
    def index() -> Response:
        return Response(
            '<html><head><title>stub</title></head><body><a href="/relative-link">rel</a></body></html>',
            mimetype="text/html",
        )

    @stub.route("/plain")
    def plain() -> Response:
        return Response("hello", mimetype="text/plain")

    @stub.route("/setcookie")
    def setcookie() -> Response:
        response = Response("ok", mimetype="text/plain")
        response.headers["Set-Cookie"] = "sid=abc; Path=/"
        return response

    @stub.route("/json")
    def json_endpoint() -> Response:
        return Response('{"ok": true}', mimetype="application/json")

    @stub.route("/echo-query")
    def echo_query() -> Response:
        return Response(
            '{"query": "' + request.query_string.decode() + '"}',
            mimetype="application/json",
        )

    @stub.route("/events")
    def sse_endpoint() -> Response:
        def gen() -> Iterator[bytes]:
            yield b"data: chunk-1\n\n"
            yield b"data: chunk-2\n\n"

        return Response(gen(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache"})

    @sock.route("/ws-echo")
    def ws_echo(ws: simple_websocket.Server) -> None:
        is_connected = True
        while is_connected:
            try:
                msg = ws.receive()
            except simple_websocket.ConnectionClosed:
                is_connected = False
            else:
                ws.send(f"echo:{msg}")

    @sock.route("/ws-server-close")
    def ws_server_close(ws: simple_websocket.Server) -> None:
        # Accept then immediately close from the backend side, without the
        # client ever sending anything. Exercises the proxy path where the
        # backend->client direction finishes first while client->backend is
        # still parked on receive().
        ws.close()

    return stub


@pytest.fixture
def stub_backend() -> Iterator[ServedApp]:
    """Start the stub backend on an ephemeral port and yield its ServedApp handle."""
    with serve_app(_build_stub_backend()) as served:
        yield served


@pytest.fixture
def workspace_app_with_stub(stub_backend: ServedApp, monkeypatch: pytest.MonkeyPatch) -> Flask:
    """Build a system_interface Flask app wired to a stub backend under service 'web'.

    Injects a pre-built ``AgentManager`` seeded with the stub's URL as the
    'web' service. The real ``mngr observe`` pipeline is not started, so the
    test doesn't need a live mngr host; service discovery is whatever we
    put in ``_applications``.
    """
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-test")

    broadcaster = WebSocketBroadcaster()
    agent_manager = AgentManager.build(broadcaster)
    agent_manager._applications = [ApplicationEntry(name="web", url=stub_backend.http_url)]

    return create_application(Config(), agent_manager=agent_manager)


@pytest.fixture
def workspace_client(workspace_app_with_stub: Flask) -> FlaskClient:
    return workspace_app_with_stub.test_client()


@pytest.fixture
def workspace_served(workspace_app_with_stub: Flask) -> Iterator[ServedApp]:
    with serve_app(workspace_app_with_stub) as served:
        yield served


def test_service_sw_js_is_served_without_stub(workspace_client: FlaskClient) -> None:
    """The scoped service worker is served statically from the system_interface."""
    response = workspace_client.get("/service/web/__sw.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/javascript")
    assert "const PREFIX = '/service/web'" in response.text


def test_first_navigation_returns_bootstrap_when_sw_cookie_missing(workspace_client: FlaskClient) -> None:
    """First HTML navigation without the sw_installed cookie gets the bootstrap page."""
    response = workspace_client.get(
        "/service/web/",
        headers={"sec-fetch-mode": "navigate"},
    )
    assert response.status_code == 200
    assert "serviceWorker.register" in response.text


def test_forwarded_html_has_base_tag_and_ws_shim(workspace_client: FlaskClient) -> None:
    """Once the SW cookie is present, HTML responses from the backend get rewritten."""
    workspace_client.set_cookie("sw_installed_web", "1")
    response = workspace_client.get(
        "/service/web/",
        headers={"sec-fetch-mode": "navigate"},
    )
    assert response.status_code == 200
    assert '<base href="/service/web/">' in response.text
    assert "OrigWebSocket" in response.text


def test_forwarded_absolute_href_is_rewritten(workspace_client: FlaskClient) -> None:
    """Absolute-path attributes in HTML are rewritten to the service prefix."""
    workspace_client.set_cookie("sw_installed_web", "1")
    response = workspace_client.get("/service/web/")
    assert 'href="/service/web/relative-link"' in response.text


def test_forwarded_plain_text_is_unchanged(workspace_client: FlaskClient) -> None:
    """Non-HTML responses pass through as-is."""
    workspace_client.set_cookie("sw_installed_web", "1")
    response = workspace_client.get("/service/web/plain")
    assert response.status_code == 200
    assert response.text == "hello"


def test_forwarded_json_is_unchanged(workspace_client: FlaskClient) -> None:
    """JSON responses pass through as-is."""
    workspace_client.set_cookie("sw_installed_web", "1")
    response = workspace_client.get("/service/web/json")
    assert response.status_code == 200
    assert response.get_json() == {"ok": True}


def test_set_cookie_is_rewritten_to_service_path(workspace_client: FlaskClient) -> None:
    """Set-Cookie headers are scoped under /service/<name>/ so services don't pollute the origin."""
    workspace_client.set_cookie("sw_installed_web", "1")
    response = workspace_client.get("/service/web/setcookie")
    assert response.status_code == 200
    set_cookie = response.headers.get("set-cookie", "")
    assert "Path=/service/web/" in set_cookie


def test_unknown_service_returns_loading_page_for_html(workspace_client: FlaskClient) -> None:
    """Unknown service with HTML accept gets the auto-retrying loading page."""
    response = workspace_client.get(
        "/service/nonexistent/",
        headers={"accept": "text/html"},
    )
    assert response.status_code == 200
    assert "Loading..." in response.text
    assert "location.reload" in response.text


def test_unknown_service_returns_502_for_non_html(workspace_client: FlaskClient) -> None:
    """Unknown service for a non-HTML request returns 502 immediately."""
    response = workspace_client.get(
        "/service/nonexistent/api",
        headers={"accept": "application/json"},
    )
    assert response.status_code == 502


def test_forwarded_query_string_reaches_backend(workspace_client: FlaskClient) -> None:
    """Query string on the incoming request is preserved in the backend URL."""
    workspace_client.set_cookie("sw_installed_web", "1")
    response = workspace_client.get("/service/web/echo-query?foo=bar&baz=qux")
    assert response.status_code == 200
    assert response.get_json()["query"] == "foo=bar&baz=qux"


def test_forwarded_sse_is_streamed(workspace_client: FlaskClient) -> None:
    """An SSE request (accept: text/event-stream) streams chunks back to the client."""
    workspace_client.set_cookie("sw_installed_web", "1")
    response = workspace_client.get(
        "/service/web/events",
        headers={"accept": "text/event-stream"},
    )
    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")
    body = response.text
    assert "chunk-1" in body
    assert "chunk-2" in body


@pytest.mark.timeout(15)
def test_websocket_echo_forwards_bidirectionally(workspace_served: ServedApp) -> None:
    """The WS dispatcher byte-forwards messages between client and backend service."""
    ws = open_ws(workspace_served, "/service/web/ws-echo")
    try:
        ws.send("hello")
        assert ws.receive(timeout=_WS_RECEIVE_TIMEOUT) == "echo:hello"
        ws.send("world")
        assert ws.receive(timeout=_WS_RECEIVE_TIMEOUT) == "echo:world"
    finally:
        close_ws(ws)


@pytest.mark.timeout(15)
def test_websocket_echoes_client_subprotocol(workspace_served: ServedApp) -> None:
    """The proxy echoes the client's offered WS subprotocol back in the handshake.

    ttyd's browser client opens its socket with the ``tty`` subprotocol, and
    Chrome aborts the handshake (close 1006, "press enter to reconnect") if the
    server's 101 response does not echo it. The proxy must reflect the offered
    subprotocol so the negotiated value is ``tty``, not ``None``.
    """
    ws = open_ws(workspace_served, "/service/web/ws-echo", subprotocols=["tty"])
    try:
        assert ws.subprotocol == "tty"
        ws.send("hello")
        assert ws.receive(timeout=_WS_RECEIVE_TIMEOUT) == "echo:hello"
    finally:
        close_ws(ws)


@pytest.mark.timeout(15)
def test_broadcaster_websocket_connects_without_subprotocol(workspace_served: ServedApp) -> None:
    """A client offering no subprotocol still connects and gets none echoed.

    Guards against the subprotocol passthrough regressing the broadcaster
    ``/api/ws`` and proto-agent-logs streams, which (unlike ttyd) never offer a
    subprotocol and must keep negotiating ``None``.
    """
    ws = open_ws(workspace_served, "/api/ws")
    try:
        assert ws.subprotocol is None
        # The broadcaster pushes an initial agents snapshot on connect.
        assert ws.receive(timeout=_WS_RECEIVE_TIMEOUT) is not None
    finally:
        close_ws(ws)


@pytest.mark.timeout(15)
def test_websocket_backend_close_propagates_to_client(workspace_served: ServedApp) -> None:
    """When the backend WS closes first, the proxy must cancel the still-parked
    client->backend direction and close the client socket rather than hanging
    forever (issue E)."""
    ws = open_ws(workspace_served, "/service/web/ws-server-close")
    try:
        # The client never sends; the proxy must propagate the backend-initiated
        # close to the client instead of hanging on the client->backend receive.
        with pytest.raises(simple_websocket.ConnectionClosed):
            ws.receive(timeout=_WS_RECEIVE_TIMEOUT)
    finally:
        close_ws(ws)


@pytest.mark.timeout(15)
def test_websocket_unknown_service_closes_with_4004(workspace_served: ServedApp) -> None:
    """A WS upgrade against an unregistered service gets closed with 4004."""
    ws = open_ws(workspace_served, "/service/nonexistent/anything")
    try:
        with pytest.raises(simple_websocket.ConnectionClosed) as excinfo:
            ws.receive(timeout=_WS_RECEIVE_TIMEOUT)
        assert excinfo.value.reason == 4004
    finally:
        close_ws(ws)


@pytest.mark.timeout(15)
def test_connect_backend_websocket_falls_back_across_addresses(stub_backend: ServedApp) -> None:
    """The backend WS connect iterates resolved addresses instead of only trying the first.

    The stub listener binds IPv4 ``127.0.0.1`` only (like ttyd). Connecting via a
    ``localhost`` URL on a dual-stack host resolves ``::1`` first, which would be
    refused; the helper must fall back to ``127.0.0.1`` and connect. (On a
    single-stack host ``localhost`` resolves straight to ``127.0.0.1`` and the
    first attempt already succeeds -- either way the connection works.)
    """
    backend_ws = _connect_backend_websocket(f"ws://localhost:{stub_backend.port}/ws-echo", None)
    try:
        backend_ws.send("ping")
        assert backend_ws.receive(timeout=_WS_RECEIVE_TIMEOUT) == "echo:ping"
    finally:
        close_ws(backend_ws)
