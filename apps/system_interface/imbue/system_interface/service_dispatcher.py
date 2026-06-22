"""Route handlers for ``/service/<name>/...`` forwarding inside system_interface.

Mirrors the pattern used by the desktop client's ``/forwarding/...`` routes
but strictly local (all target services run on 127.0.0.1 inside the same
workspace, so no SSH tunnel logic is needed) and without agent-id in the
path (one workspace per system_interface process).

Responsibilities:
- First-navigation HTML requests serve a bootstrap page that registers a
  scoped service worker at ``/service/<name>/``. The SW then transparently
  prepends the prefix to fetches issued by the service's own frontend.
- Subsequent HTTP requests forward to the backend, rewriting absolute
  paths in HTML and scoping ``Set-Cookie`` headers under the prefix.
- WebSocket requests forward bidirectionally with subprotocol passthrough.
- Requests for unknown or not-yet-registered services show the
  auto-retrying loading page (HTML accept) or return 502 (otherwise).

Everything here is synchronous: HTTP forwarding uses a sync ``httpx.Client``
and the WebSocket bridge runs one thread per direction (each WS connection
already owns its own thread under the threaded WSGI server), so there is no
asyncio anywhere.
"""

import socket
import threading
from collections.abc import Iterator
from typing import Any
from typing import Final
from urllib.parse import urlsplit
from urllib.parse import urlunsplit

import httpx
import simple_websocket
from flask import Flask
from flask import Response
from flask import request
from loguru import logger
from simple_websocket import ConnectionClosed

from imbue.system_interface.app_context import get_state
from imbue.system_interface.primitives import ServiceName
from imbue.system_interface.proxy import generate_backend_loading_html
from imbue.system_interface.proxy import generate_bootstrap_html
from imbue.system_interface.proxy import generate_service_worker_js
from imbue.system_interface.proxy import rewrite_cookie_path
from imbue.system_interface.proxy import rewrite_proxied_html

_PROXY_TIMEOUT_SECONDS: Final[float] = 30.0

_EXCLUDED_RESPONSE_HEADERS: Final[frozenset[str]] = frozenset(
    {
        "transfer-encoding",
        "content-encoding",
        "content-length",
    }
)


class BackendWebSocketUrlError(ValueError):
    """Raised when a backend WebSocket URL cannot be parsed into a host and port."""


def _sw_cookie_name(service_name: str) -> str:
    return f"sw_installed_{service_name}"


def _make_loading_html(current_service: ServiceName) -> str:
    agent_manager = get_state().agent_manager
    other_services = tuple(
        ServiceName(name) for name in agent_manager.list_service_names() if name != str(current_service)
    )
    return generate_backend_loading_html(
        current_service=current_service,
        other_services=other_services,
    )


def _forward_http_request(
    backend_url: str,
    path: str,
    service_name: str,
    http_client: httpx.Client,
) -> httpx.Response | Response:
    """Forward an HTTP request to the backend, returning the backend response or an error Response."""
    proxy_url = f"{backend_url.rstrip('/')}/{path}"
    if request.query_string:
        proxy_url = f"{proxy_url}?{request.query_string.decode()}"

    headers = dict(request.headers)
    headers.pop("Host", None)
    headers.pop("host", None)

    body = request.get_data()

    try:
        return http_client.request(
            method=request.method,
            url=proxy_url,
            headers=headers,
            content=body,
        )
    except httpx.ConnectError:
        logger.warning("Backend connection refused for service {}", service_name)
        return Response("Backend connection refused", status=502)
    except httpx.ReadError:
        logger.warning("Backend connection lost for service {}", service_name)
        return Response("Backend connection lost", status=502)
    except httpx.RemoteProtocolError:
        logger.warning("Backend disconnected without response for service {} (likely still starting)", service_name)
        return Response("Backend disconnected without response", status=502)
    except httpx.TimeoutException:
        logger.warning("Backend request timed out for service {}", service_name)
        return Response("Backend request timed out", status=504)


def _forward_http_request_streaming(
    backend_url: str,
    path: str,
    service_name: str,
    http_client: httpx.Client,
) -> Response:
    """Forward an HTTP request and stream the response back without buffering.

    Used for SSE (Server-Sent Events) endpoints where the backend sends data
    incrementally and the client needs to receive it as it arrives. The
    backend's status code and Content-Type are propagated so that a backend
    responding with something other than ``text/event-stream`` (e.g. chunked
    ``application/x-ndjson``) still renders correctly client-side.
    """
    proxy_url = f"{backend_url.rstrip('/')}/{path}"
    if request.query_string:
        proxy_url = f"{proxy_url}?{request.query_string.decode()}"

    headers = dict(request.headers)
    headers.pop("Host", None)
    headers.pop("host", None)

    body = request.get_data()

    backend_request = http_client.build_request(
        method=request.method,
        url=proxy_url,
        headers=headers,
        content=body,
    )
    try:
        backend_response = http_client.send(backend_request, stream=True)
    except httpx.ConnectError as e:
        logger.warning("Backend connection refused for service {} (streaming): {}", service_name, e)
        return Response("Backend connection refused", status=502)
    except httpx.TimeoutException as e:
        logger.warning("Backend stream timed out for service {}: {}", service_name, e)
        return Response("Backend stream timed out", status=504)

    media_type = backend_response.headers.get("content-type", "text/event-stream")
    return Response(
        _iter_backend_stream(backend_response, service_name),
        status=backend_response.status_code,
        mimetype=media_type,
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _iter_backend_stream(backend_response: httpx.Response, service_name: str) -> Iterator[bytes]:
    """Yield the backend response body chunk by chunk, closing it when done."""
    try:
        for chunk in backend_response.iter_bytes():
            yield chunk
    except httpx.ReadError as e:
        logger.warning("Backend read error during streaming for service {}: {}", service_name, e)
    except httpx.RemoteProtocolError as e:
        logger.warning("Backend disconnected without response during streaming for service {}: {}", service_name, e)
    except httpx.TimeoutException as e:
        logger.warning("Backend stream timed out for service {}: {}", service_name, e)
    finally:
        backend_response.close()


def _build_proxy_response(
    backend_response: httpx.Response,
    service_name: ServiceName,
) -> Response:
    """Transform a backend httpx response into a Flask Response with header/content rewriting."""
    header_pairs: list[tuple[str, str]] = []
    for header_key, header_value in backend_response.headers.multi_items():
        if header_key.lower() in _EXCLUDED_RESPONSE_HEADERS:
            continue
        if header_key.lower() == "set-cookie":
            header_value = rewrite_cookie_path(
                set_cookie_header=header_value,
                service_name=service_name,
            )
        header_pairs.append((header_key, header_value))

    content: str | bytes = backend_response.content

    content_type = backend_response.headers.get("content-type", "")
    if "text/html" in content_type:
        html_text = backend_response.text
        rewritten_html = rewrite_proxied_html(
            html_content=html_text,
            service_name=service_name,
        )
        content = rewritten_html.encode()

    response = Response(content, status=backend_response.status_code)
    # Carry the backend headers over. Content-Type is *replaced* (Flask seeds a
    # default, and a duplicate would leave the body mislabeled, e.g. JSON
    # served as text/html); every other header (including multiple Set-Cookie
    # entries) is appended.
    for header_key, header_value in header_pairs:
        if header_key.lower() == "content-type":
            response.headers["Content-Type"] = header_value
        else:
            response.headers.add(header_key, header_value)
    return response


def _handle_service_sw_js(service_name: str) -> Response:
    """Serve the scoped service worker script for a service."""
    return Response(
        generate_service_worker_js(ServiceName(service_name)),
        mimetype="application/javascript",
    )


def _handle_service_http(service_name: str, path: str) -> Response:
    """Handle an HTTP request under ``/service/<name>/<path>``."""
    parsed_service = ServiceName(service_name)
    agent_manager = get_state().agent_manager

    if path == "__sw.js":
        return _handle_service_sw_js(service_name)

    is_navigation = request.headers.get("sec-fetch-mode") == "navigate"

    backend_url = agent_manager.get_service_url(service_name)
    if backend_url is None:
        accept = request.headers.get("accept", "")
        if "text/html" in accept:
            return Response(_make_loading_html(parsed_service), mimetype="text/html")
        return Response(f"Service '{service_name}' not registered", status=502)

    sw_cookie = request.cookies.get(_sw_cookie_name(service_name))

    if is_navigation and not sw_cookie:
        return Response(generate_bootstrap_html(parsed_service), mimetype="text/html")

    http_client: httpx.Client = get_state().http_client

    accept = request.headers.get("accept", "")
    is_likely_sse = "text/event-stream" in accept

    if is_likely_sse:
        return _forward_http_request_streaming(
            backend_url=backend_url,
            path=path,
            service_name=service_name,
            http_client=http_client,
        )

    result = _forward_http_request(
        backend_url=backend_url,
        path=path,
        service_name=service_name,
        http_client=http_client,
    )

    if isinstance(result, Response):
        if result.status_code >= 500 and "text/html" in request.headers.get("accept", ""):
            return Response(_make_loading_html(parsed_service), mimetype="text/html")
        return result

    return _build_proxy_response(
        backend_response=result,
        service_name=parsed_service,
    )


def _forward_client_to_backend(
    client_websocket: Any,
    backend_ws: simple_websocket.Client,
    stop_event: threading.Event,
) -> None:
    """Forward messages from the client WebSocket to the backend until either side closes."""
    try:
        while not stop_event.is_set():
            data = client_websocket.receive(timeout=1.0)
            if data is None:
                # Receive timed out; re-check the stop flag and keep going.
                continue
            backend_ws.send(data)
    except ConnectionClosed:
        logger.trace("Client WebSocket disconnected")
    finally:
        stop_event.set()
        try:
            backend_ws.close()
        except ConnectionClosed:
            logger.trace("Backend WebSocket already closed during client->backend cleanup")


def _forward_backend_to_client(
    client_websocket: Any,
    backend_ws: simple_websocket.Client,
    service_name: str,
    stop_event: threading.Event,
) -> None:
    """Forward messages from the backend WebSocket to the client until either side closes."""
    try:
        while not stop_event.is_set():
            message = backend_ws.receive(timeout=1.0)
            if message is None:
                continue
            client_websocket.send(message)
    except ConnectionClosed:
        logger.debug("Backend WebSocket closed for service {}", service_name)
    finally:
        stop_event.set()
        try:
            client_websocket.close()
        except ConnectionClosed:
            logger.trace("Client WebSocket already closed during backend->client cleanup")


def _connect_backend_websocket(ws_url: str, subprotocols: list[str] | None) -> simple_websocket.Client:
    """Open a WebSocket to the backend, trying every resolved address in turn.

    ``simple_websocket.Client`` connects only to ``getaddrinfo(...)[0]`` and does
    not fall back. In the container, ``localhost`` resolves to IPv6 ``::1``
    first, but the backend services (ttyd, etc.) bind IPv4 ``127.0.0.1`` only, so
    that single attempt is refused. We resolve the host ourselves and connect to
    the first address that accepts, pinning each candidate URL to that concrete
    IP -- the address fallback ``httpx`` and the old ``websockets`` client did
    for free.
    """
    parsed = urlsplit(ws_url)
    host = parsed.hostname
    port = parsed.port
    if host is None or port is None:
        raise BackendWebSocketUrlError(f"backend WebSocket URL is missing host or port: {ws_url}")

    last_error: Exception | None = None
    for family, _socktype, _proto, _canonname, sockaddr in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        address = sockaddr[0]
        host_for_url = f"[{address}]" if family == socket.AF_INET6 else address
        candidate_url = urlunsplit(
            (parsed.scheme, f"{host_for_url}:{port}", parsed.path, parsed.query, parsed.fragment)
        )
        try:
            return simple_websocket.Client(candidate_url, subprotocols=subprotocols)
        except (ConnectionRefusedError, ConnectionError, OSError, TimeoutError, ConnectionClosed) as error:
            last_error = error
            logger.trace("Backend WebSocket connect to {} failed, trying next address: {}", address, error)
    if last_error is not None:
        raise last_error
    raise ConnectionError(f"no addresses resolved for backend WebSocket {host}:{port}")


def _handle_service_websocket(
    client_websocket: Any,
    service_name: str,
    path: str,
) -> None:
    """Proxy a WebSocket connection under ``/service/<name>/<path>`` to the backend service."""
    agent_manager = get_state().agent_manager

    backend_url = agent_manager.get_service_url(service_name)
    if backend_url is None:
        client_websocket.close(4004, f"Unknown service: {service_name}")
        return

    ws_backend = backend_url.replace("http://", "ws://").replace("https://", "wss://").rstrip("/")
    ws_url = f"{ws_backend}/{path}"
    if request.query_string:
        ws_url = f"{ws_url}?{request.query_string.decode()}"

    client_subprotocol_header = request.headers.get("sec-websocket-protocol")
    subprotocols: list[str] = []
    if client_subprotocol_header:
        subprotocols = [s.strip() for s in client_subprotocol_header.split(",")]

    try:
        backend_ws = _connect_backend_websocket(ws_url, subprotocols or None)
    except (ConnectionRefusedError, ConnectionError, OSError, TimeoutError, ConnectionClosed) as connection_error:
        logger.debug("Backend WebSocket connection failed for service {}: {}", service_name, connection_error)
        try:
            client_websocket.close(1011, "Backend connection failed")
        except ConnectionClosed:
            logger.trace("WebSocket already closed when trying to send error for service {}", service_name)
        return

    # Bridge both directions with one thread each. When either direction ends,
    # the stop event is set and both sockets are closed, which unblocks the
    # surviving direction's ``receive`` so it exits too.
    stop_event = threading.Event()
    backend_to_client = threading.Thread(
        target=_forward_backend_to_client,
        kwargs={
            "client_websocket": client_websocket,
            "backend_ws": backend_ws,
            "service_name": service_name,
            "stop_event": stop_event,
        },
        daemon=True,
    )
    backend_to_client.start()
    try:
        _forward_client_to_backend(
            client_websocket=client_websocket,
            backend_ws=backend_ws,
            stop_event=stop_event,
        )
    finally:
        stop_event.set()
        backend_to_client.join(timeout=_PROXY_TIMEOUT_SECONDS)


def _service_websocket_route(client_websocket: Any, service_name: str, path: str) -> None:
    _handle_service_websocket(client_websocket=client_websocket, service_name=service_name, path=path)


def register_service_routes(application: Flask, sock: Any) -> None:
    """Register ``/service/<name>/...`` HTTP + WebSocket routes on the application.

    ``sock`` is the flask-sock ``Sock`` bound to ``application``. werkzeug routes
    the WebSocket-upgrade requests to the sock rule (registered with
    ``websocket=True``) and plain HTTP requests to the HTTP rule, even though
    both share the same URL pattern.
    """
    http_methods = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"]
    # The trailing-slash form (``/service/web/``, empty path) must route to the
    # proxy too -- the ``<path:path>`` converter does not match an empty
    # segment, so register it explicitly with a default path.
    application.add_url_rule(
        "/service/<service_name>/",
        view_func=_handle_service_http,
        defaults={"path": ""},
        methods=http_methods,
        endpoint="_handle_service_http_root",
    )
    application.add_url_rule(
        "/service/<service_name>/<path:path>",
        view_func=_handle_service_http,
        methods=http_methods,
    )
    sock.route("/service/<service_name>/<path:path>")(_service_websocket_route)
