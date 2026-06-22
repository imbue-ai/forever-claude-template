"""Minimal example Flask web server.

Serves a single placeholder page so the "web" application slot in the
desktop client has something meaningful to render out of the box.
Registration with ``runtime/applications.toml`` is handled by the
``web`` supervisord program (via ``scripts/forward_port.py``) so the
app-watcher writes the service_registered event to ``events/services/events.jsonl``.
"""

import os

from flask import Flask, Response
from werkzeug.serving import run_simple

WEB_SERVER_PORT = int(os.environ.get("WEB_SERVER_PORT", "8080"))

app = Flask(__name__, static_folder=None)

_PLACEHOLDER_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <title>Example web server</title>
  <style>
    body { font-family: system-ui, -apple-system, sans-serif; padding: 48px; color: #1e293b; }
    h1 { margin-bottom: 16px; }
    p { max-width: 640px; line-height: 1.5; color: #334155; }
    code { background: #f1f5f9; padding: 2px 6px; border-radius: 4px; }
  </style>
</head>
<body>
  <h1>Example web server</h1>
  <p>
    This is an example web server. You can use it for whatever you want!
  </p>
  <p>
    The source lives at <code>libs/web_server/</code> in your project.
    Edit <code>runner.py</code> to add your own routes, or swap it out for
    any other web app by pointing the <code>web</code> program in
    <code>supervisord.conf</code> at a different command.
  </p>
</body>
</html>
"""


@app.route("/")
def index() -> Response:
    return Response(_PLACEHOLDER_HTML, mimetype="text/html")


@app.route("/health")
def health() -> Response:
    return Response('{"status": "ok"}', mimetype="application/json")


def main() -> None:
    run_simple(
        "127.0.0.1",
        WEB_SERVER_PORT,
        app,
        threaded=True,
        use_reloader=False,
        use_debugger=False,
    )


if __name__ == "__main__":
    main()
