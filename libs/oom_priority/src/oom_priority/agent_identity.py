"""Resolve whether an agent is a worker (created by another agent) or a
user-created agent, from the host's agent records.

mngr writes one record per agent at ``$MNGR_HOST_DIR/agents/<id>/data.json``
carrying the agent ``name`` and a ``labels`` dict. The agent-creation paths label
worker creations ``agent_created=true`` and user-facing creations
``user_created=true``; this maps a name to the right priority band.

An agent we cannot classify defaults to *not* a worker -- i.e. it is tagged at
the more-protected user-agent band, so an unlabeled agent is shed later rather
than earlier.

Stdlib-only (see ``paths``): imported by the agent-tagging Claude hook under a
plain ``python3``.
"""

import json
import os
from pathlib import Path


def is_worker_agent(agent_name: str) -> bool:
    """Whether ``agent_name`` carries the ``agent_created=true`` label.

    Returns False when the host records are unavailable or the agent is not
    found, so the caller falls back to the protected user-agent band.
    """
    host_dir = os.environ.get("MNGR_HOST_DIR", "")
    if not host_dir:
        return False
    agents_dir = Path(host_dir) / "agents"
    if not agents_dir.is_dir():
        return False
    for agent_dir in agents_dir.iterdir():
        data_path = agent_dir / "data.json"
        if not data_path.exists():
            continue
        try:
            data = json.loads(data_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("name") != agent_name:
            continue
        labels = data.get("labels")
        if not isinstance(labels, dict):
            return False
        return str(labels.get("agent_created", "")).lower() == "true"
    return False
