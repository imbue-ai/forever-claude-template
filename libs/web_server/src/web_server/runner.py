"""Minimal example FastAPI web server.

Serves a single placeholder page so the "web" application slot in the
desktop client has something meaningful to render out of the box.
Registration with ``runtime/applications.toml`` is handled by the
``services.toml`` entry (via ``scripts/forward_port.py``) so the
app-watcher writes the service_registered event to ``events/services/events.jsonl``.
"""

import os

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

WEB_SERVER_PORT = int(os.environ.get("WEB_SERVER_PORT", "8080"))

app = FastAPI(title="Example web server")

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
    any other FastAPI/ASGI app by pointing <code>services.toml</code> at
    a different command.
  </p>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(_PLACEHOLDER_HTML)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def main() -> None:
    uvicorn.run(app, host="127.0.0.1", port=WEB_SERVER_PORT)


if __name__ == "__main__":
    main()
