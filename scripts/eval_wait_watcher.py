"""Watch the workspace chat agent's WAITING state via the local system_interface, and send
messages the way the UI chat box does. Pure stdlib; loopback to 127.0.0.1:8000 (unauthenticated
inside the sandbox)."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

SYSTEM_INTERFACE = "http://127.0.0.1:8000"
_POLL_SECONDS = 3.0


def _get_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def resolve_chat_agent_id(deadline: float) -> str | None:
    """The chat (primary) agent id is persisted by the manager at $MNGR_HOST_DIR/initial_chat_agent_id."""
    id_path = Path(os.environ.get("MNGR_HOST_DIR", "/mngr")) / "initial_chat_agent_id"
    while time.time() < deadline:
        if id_path.is_file():
            value = id_path.read_text().strip()
            if value:
                return value
        time.sleep(_POLL_SECONDS)
    return None


def agent_state(agent_id: str) -> str | None:
    try:
        agents = _get_json("{}/api/agents".format(SYSTEM_INTERFACE)).get("agents", [])
    except (urllib.error.URLError, OSError, ValueError):
        return None
    for agent in agents:
        if agent.get("id") == agent_id:
            return (agent.get("state") or "").upper()
    return None


def wait_until(agent_id: str, *, waiting: bool, deadline: float) -> bool:
    """Block until the agent is WAITING (waiting=True) or has left WAITING (waiting=False)."""
    while time.time() < deadline:
        state = agent_state(agent_id)
        if state is not None and (state == "WAITING") == waiting:
            return True
        time.sleep(_POLL_SECONDS)
    return False


def send_message(agent_id: str, message: str, deadline: float) -> bool:
    body = json.dumps({"message": message}).encode("utf-8")
    while time.time() < deadline:
        request = urllib.request.Request(
            "{}/api/agents/{}/message".format(SYSTEM_INTERFACE, agent_id),
            data=body, headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(_POLL_SECONDS)
    return False


def fetch_all_events(agent_id: str) -> list[dict]:
    """Full conversation history from the local system_interface events endpoint."""
    total = _get_json("{}/api/agents/{}/events?offset=0&limit=1".format(SYSTEM_INTERFACE, agent_id)).get("total", 0)
    data = _get_json("{}/api/agents/{}/events?offset=0&limit={}".format(SYSTEM_INTERFACE, agent_id, max(total, 1)))
    return data.get("events", [])
