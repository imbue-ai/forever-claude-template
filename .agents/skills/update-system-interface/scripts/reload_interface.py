#!/usr/bin/env python3
"""Tell the running system interface to reload its entire UI in the browser.

This is the frontend-reveal step of the ``update-system-interface`` flow: the
lead agent runs ``npm run build`` to regenerate the (gitignored) static bundle,
then runs this script to make the user's open workspace reload the new bundle.

Unlike ``scripts/layout.py refresh`` -- which reloads a single inner iframe or
all iframes for one service -- this reloads the *top-level* page that hosts the
dockview shell. A full page reload is the only thing that picks up new hashed
assets AND any change to the shell chrome itself, and it transitively reloads
every child chat iframe.

Mechanism: POST ``{op: "reload_interface", args: {}, agent_id}`` to the
loopback-only ``/api/layout/broadcast`` endpoint on the workspace server, which
relays a ``layout_op`` WebSocket message to the connected browser. If no browser
is connected the broadcast is a harmless no-op, matching how the layout
broadcasts behave.

This script is intentionally separate from ``scripts/layout.py`` -- not because
that helper is purely about arranging panels (its ``refresh`` op already does a
non-arranging reload), but because of ownership and blast radius: ``layout.py``
is a general agent-facing surface exposed via the ``manage-layout`` skill that
any agent may invoke at any time, whereas a full-UI reload is a privileged step
in one specific lead-only reveal sequence that should fire only after a verified
rebuild. Keeping it out of the general layout CLI prevents it from showing up as
a casually-invokable panel verb. It lives with the skill that owns the flow and
is not meant to be used by other processes.

The HTTP/env plumbing below is deliberately a small standalone copy of
``scripts/layout.py``'s ``_post_layout`` (same endpoint, env vars, header, and
default port). The two live in different directories, so a shared import would
be awkward; if the workspace-server URL/port convention ever changes, update
both.

Environment:
    MINDS_WORKSPACE_SERVER_URL  Base URL of the workspace server
                                (default http://127.0.0.1:8000).
    MNGR_AGENT_ID               Sent for telemetry (body + X-Mngr-Agent-Id).

Exit codes: 0 on a successful broadcast; 1 if the server could not be reached
or returned an error.
"""

import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_WORKSPACE_URL = "http://127.0.0.1:8000"
ENV_WORKSPACE_URL = "MINDS_WORKSPACE_SERVER_URL"
ENV_MNGR_AGENT_ID = "MNGR_AGENT_ID"
MNGR_AGENT_ID_HEADER = "X-Mngr-Agent-Id"
_OP = "reload_interface"


def _workspace_base_url() -> str:
    return os.environ.get(ENV_WORKSPACE_URL, DEFAULT_WORKSPACE_URL).rstrip("/")


def _mngr_agent_id() -> str:
    return os.environ.get(ENV_MNGR_AGENT_ID, "")


def main() -> int:
    url = f"{_workspace_base_url()}/api/layout/broadcast"
    agent_id = _mngr_agent_id()
    body = json.dumps({"op": _OP, "args": {}, "agent_id": agent_id}).encode("utf-8")
    headers = {"Content-Type": "application/json", MNGR_AGENT_ID_HEADER: agent_id}
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10.0) as response:
            status = response.status
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        sys.stderr.write(
            f"error: reload_interface rejected (HTTP {e.code}): {detail}\n"
        )
        return 1
    except urllib.error.URLError as e:
        sys.stderr.write(
            f"error: could not reach workspace server at {url}: {e.reason}\n"
        )
        return 1

    if status != 200:
        sys.stderr.write(f"error: reload_interface failed (HTTP {status}): {raw}\n")
        return 1

    sys.stderr.write(
        "reload_interface broadcast sent; any connected browser will reload the whole interface "
        "(no-op if no browser is connected).\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
