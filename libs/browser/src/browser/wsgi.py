"""Tiny threaded HTTP/1.1 Werkzeug server, copied from apps/system_interface.

Copied verbatim (not imported) because ``libs/browser`` must not depend on an
app. The browser daemon serves Flask + flask-sock over a thread-per-connection
server: every long-lived NDJSON stream and every cast WebSocket owns its own OS
thread, which is what flask-sock needs and what replaces uvicorn's single
asyncio event loop. HTTP/1.1 (vs werkzeug's HTTP/1.0 default) is required for
keepalive and the incremental flush of the streamed ``task``/``hold`` responses.
"""

from flask import Flask
from werkzeug.serving import BaseWSGIServer, WSGIRequestHandler, make_server


class Http11RequestHandler(WSGIRequestHandler):
    """Werkzeug request handler pinned to HTTP/1.1.

    The default dev-server handler speaks HTTP/1.0, which disables keepalive and
    forces a connection close after each response -- breaking long-lived NDJSON
    streams and the per-connection keepalive that the WebSocket endpoint relies
    on. HTTP/1.1 enables persistent connections and chunked transfer encoding so
    streamed responses flush incrementally.
    """

    protocol_version = "HTTP/1.1"


def make_threaded_server(host: str, port: int, app: Flask) -> BaseWSGIServer:
    """Build a threaded Werkzeug server that serves HTTP/1.1.

    Thread-per-connection (so flask-sock and long-lived NDJSON connections each
    own a thread) plus HTTP/1.1 keepalive and chunked streaming. The caller owns
    the returned server's ``serve_forever`` / ``shutdown`` lifecycle.
    """
    return make_server(host, port, app, threaded=True, request_handler=Http11RequestHandler)
