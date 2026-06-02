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
"""

import asyncio
from collections.abc import AsyncGenerator
from typing import Final

import httpx
import websockets
import websockets.asyncio.client
from fastapi import FastAPI
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.responses import Response
from fastapi.responses import StreamingResponse
from loguru import logger
from starlette.websockets import WebSocket
from starlette.websockets import WebSocketDisconnect
from websockets import ClientConnection

from imbue.system_interface.agent_manager import AgentManager
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

# Request headers that describe the *original* framing of the client request.
# We re-stream the body to the backend as a chunked async iterator, so httpx
# sets its own Transfer-Encoding and a stale Content-Length/Transfer-Encoding
# from the client would conflict. ``host`` is dropped so the backend sees its
# own host.
_EXCLUDED_REQUEST_HEADERS: Final[frozenset[str]] = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
    }
)

# Hard cap on a forwarded *request* body. The body is streamed to the backend
# (never buffered in the proxy), so this is an abuse bound rather than an
# OOM guard: a client that streams more than this many bytes gets a 413 and
# the upstream send is aborted. Generous enough for ordinary file uploads to
# a workspace service.
_MAX_REQUEST_BODY_BYTES: Final[int] = 2 * 1024 * 1024 * 1024

# Hard cap on a *response* body that we must buffer in order to rewrite it
# (i.e. HTML, which needs the <base> tag / WS shim / absolute-path rewriting).
# Non-HTML responses are streamed through without buffering and are not capped,
# so large downloads still work; only the rewrite path holds bytes in memory.
_MAX_REWRITABLE_HTML_BYTES: Final[int] = 25 * 1024 * 1024

# Chunk size for streaming bodies in either direction.
_STREAM_CHUNK_SIZE: Final[int] = 64 * 1024


class _RequestBodyTooLargeError(Exception):
    """Raised by the request-body stream wrapper when the cap is exceeded."""


def _parse_content_length(request: Request) -> int | None:
    """Return the declared Content-Length, or None if absent/unparseable."""
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _request_has_body(request: Request) -> bool:
    """Whether the incoming request carries a body worth forwarding.

    Browsers set Content-Length on bodied requests and use chunked
    Transfer-Encoding for streamed uploads. Bodyless requests (the common
    GET/HEAD case) are forwarded without a content stream so httpx does not
    add a spurious chunked body.
    """
    if request.headers.get("transfer-encoding"):
        return True
    content_length = _parse_content_length(request)
    return content_length is not None and content_length > 0


async def _capped_request_stream(request: Request, max_bytes: int) -> AsyncGenerator[bytes, None]:
    """Yield the request body in chunks, aborting if it exceeds ``max_bytes``."""
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise _RequestBodyTooLargeError()
        yield chunk


def _forwarded_request_headers(request: Request) -> dict[str, str]:
    return {key: value for key, value in request.headers.items() if key.lower() not in _EXCLUDED_REQUEST_HEADERS}


def _body_too_large_response() -> Response:
    return Response(status_code=413, content="Request body too large")


def _apply_backend_headers(
    response: Response,
    backend_response: httpx.Response,
    service_name: ServiceName,
    skip_header_keys: frozenset[str],
) -> None:
    """Copy backend response headers onto ``response`` with scoping/exclusions.

    Excludes hop-by-hop/framing headers (``_EXCLUDED_RESPONSE_HEADERS``) and
    any keys in ``skip_header_keys`` (e.g. ``content-type`` when it is already
    carried by a StreamingResponse's ``media_type``). ``Set-Cookie`` is
    rewritten to scope under the service prefix.
    """
    for header_key, header_value in backend_response.headers.multi_items():
        lowered = header_key.lower()
        if lowered in _EXCLUDED_RESPONSE_HEADERS or lowered in skip_header_keys:
            continue
        if lowered == "set-cookie":
            header_value = rewrite_cookie_path(set_cookie_header=header_value, service_name=service_name)
        response.headers.append(header_key, header_value)


async def _build_rewritten_html_response(
    backend_response: httpx.Response,
    service_name: ServiceName,
    max_bytes: int,
) -> Response:
    """Buffer an HTML response (up to ``max_bytes``), rewrite it, and return it."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in backend_response.aiter_bytes(chunk_size=_STREAM_CHUNK_SIZE):
        total += len(chunk)
        if total > max_bytes:
            logger.warning(
                "HTML response from service {} exceeds {} bytes; refusing to buffer for rewrite",
                service_name,
                max_bytes,
            )
            return Response(status_code=502, content="HTML response too large to proxy")
        chunks.append(chunk)
    html_text = b"".join(chunks).decode(backend_response.encoding or "utf-8", errors="replace")
    rewritten_html = rewrite_proxied_html(html_content=html_text, service_name=service_name)

    response = Response(content=rewritten_html.encode(), status_code=backend_response.status_code)
    _apply_backend_headers(
        response=response,
        backend_response=backend_response,
        service_name=service_name,
        skip_header_keys=frozenset({"content-type"}),
    )
    response.headers["content-type"] = backend_response.headers.get("content-type", "text/html")
    return response


def _build_streaming_proxy_response(
    backend_response: httpx.Response,
    service_name: ServiceName,
) -> StreamingResponse:
    """Stream a non-HTML response body through without buffering it."""

    async def _stream_generator() -> AsyncGenerator[bytes, None]:
        try:
            async for chunk in backend_response.aiter_bytes(chunk_size=_STREAM_CHUNK_SIZE):
                yield chunk
        except httpx.ReadError as e:
            logger.warning("Backend read error during streaming for service {}: {}", service_name, e)
        except httpx.RemoteProtocolError as e:
            logger.warning(
                "Backend disconnected without response during streaming for service {}: {}", service_name, e
            )
        except httpx.TimeoutException as e:
            logger.warning("Backend stream timed out for service {}: {}", service_name, e)
        finally:
            await backend_response.aclose()

    media_type = backend_response.headers.get("content-type", "application/octet-stream")
    response = StreamingResponse(
        _stream_generator(),
        status_code=backend_response.status_code,
        media_type=media_type,
    )
    _apply_backend_headers(
        response=response,
        backend_response=backend_response,
        service_name=service_name,
        skip_header_keys=frozenset({"content-type"}),
    )
    response.headers["X-Accel-Buffering"] = "no"
    return response


async def _forward_http_request(
    request: Request,
    backend_url: str,
    path: str,
    service_name: str,
    http_client: httpx.AsyncClient,
) -> Response:
    """Forward an HTTP request to the backend, streaming both bodies.

    The request body is streamed upstream (never buffered) with a hard cap.
    The response is streamed back unless it is HTML, which must be buffered
    (up to a smaller cap) so the proxy can rewrite absolute paths and inject
    the ``<base>`` tag and WebSocket shim. Raises the httpx connection errors
    to the caller, which maps them to a loading page or 5xx.
    """
    proxy_url = f"{backend_url.rstrip('/')}/{path}"
    if request.url.query:
        proxy_url = f"{proxy_url}?{request.url.query}"

    declared_length = _parse_content_length(request)
    if declared_length is not None and declared_length > _MAX_REQUEST_BODY_BYTES:
        return _body_too_large_response()

    headers = _forwarded_request_headers(request)
    content = _capped_request_stream(request, _MAX_REQUEST_BODY_BYTES) if _request_has_body(request) else None

    backend_request = http_client.build_request(
        method=request.method,
        url=proxy_url,
        headers=headers,
        content=content,
    )
    try:
        backend_response = await http_client.send(backend_request, stream=True)
    except _RequestBodyTooLargeError:
        logger.warning("Request body to service {} exceeded {} bytes", service_name, _MAX_REQUEST_BODY_BYTES)
        return _body_too_large_response()

    parsed_service_name = ServiceName(service_name)
    content_type = backend_response.headers.get("content-type", "")
    if "text/html" in content_type:
        try:
            return await _build_rewritten_html_response(
                backend_response, parsed_service_name, _MAX_REWRITABLE_HTML_BYTES
            )
        finally:
            await backend_response.aclose()

    return _build_streaming_proxy_response(backend_response, parsed_service_name)


def _sw_cookie_name(service_name: str) -> str:
    return f"sw_installed_{service_name}"


def _make_loading_html(current_service: ServiceName, agent_manager: AgentManager) -> str:
    other_services = tuple(
        ServiceName(name) for name in agent_manager.list_service_names() if name != str(current_service)
    )
    return generate_backend_loading_html(
        current_service=current_service,
        other_services=other_services,
    )


async def _handle_service_sw_js(service_name: str) -> Response:
    """Serve the scoped service worker script for a service."""
    return Response(
        content=generate_service_worker_js(ServiceName(service_name)),
        media_type="application/javascript",
    )


async def _handle_service_http(
    service_name: str,
    path: str,
    request: Request,
) -> Response:
    """Handle an HTTP request under ``/service/<name>/<path>``."""
    parsed_service = ServiceName(service_name)
    agent_manager: AgentManager = request.app.state.agent_manager

    if path == "__sw.js":
        return await _handle_service_sw_js(service_name)

    is_navigation = request.headers.get("sec-fetch-mode") == "navigate"

    backend_url = agent_manager.get_service_url(service_name)
    if backend_url is None:
        accept = request.headers.get("accept", "")
        if "text/html" in accept:
            return HTMLResponse(content=_make_loading_html(parsed_service, agent_manager))
        return Response(status_code=502, content=f"Service '{service_name}' not registered")

    sw_cookie = request.cookies.get(_sw_cookie_name(service_name))

    if is_navigation and not sw_cookie:
        return HTMLResponse(generate_bootstrap_html(parsed_service))

    http_client: httpx.AsyncClient = request.app.state.http_client

    try:
        return await _forward_http_request(
            request=request,
            backend_url=backend_url,
            path=path,
            service_name=service_name,
            http_client=http_client,
        )
    except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as e:
        logger.warning("Backend connection error for service {}: {}", service_name, e)
        return _connection_error_response(502, "Backend connection failed", request, parsed_service, agent_manager)
    except httpx.TimeoutException as e:
        logger.warning("Backend request timed out for service {}: {}", service_name, e)
        return _connection_error_response(504, "Backend request timed out", request, parsed_service, agent_manager)


def _connection_error_response(
    status_code: int,
    detail: str,
    request: Request,
    parsed_service: ServiceName,
    agent_manager: AgentManager,
) -> Response:
    """Return the auto-retrying loading page for HTML navigations, else a 5xx.

    Connection failures usually mean the backend service is still starting; an
    HTML navigation gets the loading page (which retries), while non-HTML
    callers get the raw error status.
    """
    if "text/html" in request.headers.get("accept", ""):
        return HTMLResponse(content=_make_loading_html(parsed_service, agent_manager))
    return Response(status_code=status_code, content=detail)


async def _forward_client_to_backend(
    client_websocket: WebSocket,
    backend_ws: ClientConnection,
) -> None:
    """Forward messages from the client WebSocket to the backend."""
    connected = True
    try:
        while connected:
            data = await client_websocket.receive()
            msg_type = data.get("type", "")
            if msg_type == "websocket.disconnect":
                connected = False
            elif "text" in data:
                await backend_ws.send(data["text"])
            elif "bytes" in data:
                await backend_ws.send(data["bytes"])
            else:
                logger.trace("Ignoring WebSocket message with no text or bytes: {}", msg_type)
    except WebSocketDisconnect:
        logger.trace("Client WebSocket disconnected")
    except RuntimeError as e:
        logger.trace("Client WebSocket receive error (likely post-disconnect): {}", e)
    except websockets.exceptions.ConnectionClosed:
        logger.debug("Backend WebSocket closed while forwarding client message")

    try:
        await backend_ws.close()
    except websockets.exceptions.ConnectionClosed:
        logger.trace("Backend WebSocket already closed during cleanup")


async def _forward_backend_to_client(
    client_websocket: WebSocket,
    backend_ws: ClientConnection,
    service_name: str,
) -> None:
    """Forward messages from the backend WebSocket to the client."""
    try:
        async for msg in backend_ws:
            if isinstance(msg, str):
                await client_websocket.send_text(msg)
            else:
                await client_websocket.send_bytes(msg)
    except websockets.exceptions.ConnectionClosed:
        logger.debug("Backend WebSocket closed for service {}", service_name)
    except RuntimeError as e:
        logger.trace("Client WebSocket send error (likely post-disconnect): {}", e)


async def _handle_service_websocket(
    websocket: WebSocket,
    service_name: str,
    path: str,
) -> None:
    """Proxy a WebSocket connection under ``/service/<name>/<path>`` to the backend service."""
    agent_manager: AgentManager = websocket.app.state.agent_manager

    backend_url = agent_manager.get_service_url(service_name)
    if backend_url is None:
        await websocket.close(code=4004, reason=f"Unknown service: {service_name}")
        return

    ws_backend = backend_url.replace("http://", "ws://").replace("https://", "wss://").rstrip("/")
    ws_url = f"{ws_backend}/{path}"
    if websocket.url.query:
        ws_url = f"{ws_url}?{websocket.url.query}"

    client_subprotocol_header = websocket.headers.get("sec-websocket-protocol")
    subprotocols: list[str] = []
    if client_subprotocol_header:
        subprotocols = [s.strip() for s in client_subprotocol_header.split(",")]
    ws_subprotocols = [websockets.Subprotocol(s) for s in subprotocols] if subprotocols else None

    try:
        async with websockets.connect(ws_url, subprotocols=ws_subprotocols) as backend_ws:
            await websocket.accept(subprotocol=backend_ws.subprotocol)
            await asyncio.gather(
                _forward_client_to_backend(client_websocket=websocket, backend_ws=backend_ws),
                _forward_backend_to_client(
                    client_websocket=websocket, backend_ws=backend_ws, service_name=service_name
                ),
            )
    except (ConnectionRefusedError, OSError, TimeoutError) as connection_error:
        logger.debug("Backend WebSocket connection failed for service {}: {}", service_name, connection_error)
        try:
            await websocket.close(code=1011, reason="Backend connection failed")
        except RuntimeError:
            logger.trace("WebSocket already closed when trying to send error for service {}", service_name)


def register_service_routes(application: FastAPI) -> None:
    """Register ``/service/<name>/...`` HTTP + WebSocket routes on the application."""
    application.add_api_route(
        "/service/{service_name}/{path:path}",
        _handle_service_http,
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
    )

    @application.websocket("/service/{service_name}/{path:path}")
    async def service_websocket(websocket: WebSocket, service_name: str, path: str) -> None:
        await _handle_service_websocket(websocket=websocket, service_name=service_name, path=path)
