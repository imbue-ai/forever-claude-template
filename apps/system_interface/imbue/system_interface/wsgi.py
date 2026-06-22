from flask import Flask
from werkzeug.serving import BaseWSGIServer
from werkzeug.serving import WSGIRequestHandler
from werkzeug.serving import make_server


class Http11RequestHandler(WSGIRequestHandler):
    """Werkzeug request handler pinned to HTTP/1.1.

    The default dev-server handler speaks HTTP/1.0, which disables keepalive and
    forces a connection close after each response -- breaking long-lived
    Server-Sent Events streams and the per-connection keepalive that the
    WebSocket endpoints rely on. HTTP/1.1 enables persistent connections and
    chunked transfer encoding so streamed responses flush incrementally.
    """

    protocol_version = "HTTP/1.1"


def make_threaded_server(host: str, port: int, app: Flask) -> BaseWSGIServer:
    """Build a threaded Werkzeug server that serves HTTP/1.1.

    Thread-per-connection (so flask-sock and long-lived SSE/WebSocket
    connections each own a thread) plus HTTP/1.1 keepalive and chunked
    streaming. The caller owns the returned server's ``serve_forever`` /
    ``shutdown`` lifecycle.
    """
    return make_server(host, port, app, threaded=True, request_handler=Http11RequestHandler)
