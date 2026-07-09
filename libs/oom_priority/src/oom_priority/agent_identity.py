"""Resolve an agent's priority class -- worker, primary (services), or a plain
user-created chat -- from the host's agent records.

mngr writes one record per agent at ``$MNGR_HOST_DIR/agents/<id>/data.json``
carrying the agent ``name`` and a ``labels`` dict. The agent-creation paths label
worker creations ``agent_created=true`` and user-facing creations
``user_created=true``; the workspace's own services agent additionally carries
``is_primary=true``. This maps a name to the right priority band.

An agent we cannot classify defaults to *not* a worker and *not* primary -- i.e.
it is tagged at the more-protected user-agent band, so an unlabeled agent is shed
later rather than earlier (but not pinned to the never-shed primary band).

Stdlib-only (see ``paths``): imported by the agent-tagging Claude hook under a
plain ``python3``.
"""

import json
import os
from pathlib import Path


def _labels_for_agent(agent_name: str) -> dict | None:
    """Return the ``labels`` dict recorded for ``agent_name``, or None.

    None when the host records are unavailable, the agent is not found, or its
    record carries no ``labels`` dict -- callers treat that as "unclassified" and
    fall back to the protected user-agent band.
    """
    host_dir = os.environ.get("MNGR_HOST_DIR", "")
    if not host_dir:
        return None
    agents_dir = Path(host_dir) / "agents"
    if not agents_dir.is_dir():
        return None
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
        return labels if isinstance(labels, dict) else None
    return None


def _has_true_label(agent_name: str, label: str) -> bool:
    labels = _labels_for_agent(agent_name)
    if labels is None:
        return False
    return str(labels.get(label, "")).lower() == "true"


def is_worker_agent(agent_name: str) -> bool:
    """Whether ``agent_name`` carries the ``agent_created=true`` label.

    Returns False when the host records are unavailable or the agent is not
    found, so the caller falls back to the protected user-agent band.
    """
    return _has_true_label(agent_name, "agent_created")


def is_primary_agent(agent_name: str) -> bool:
    """Whether ``agent_name`` is the workspace's primary (services) agent.

    The primary agent runs the workspace's supervised services; shedding it would
    tear those down and make the workspace report a broken state, so it is pinned
    to the never-shed primary band. Returns False when the record is unavailable
    or unclassified, so only an agent explicitly labelled ``is_primary=true`` is
    ever pinned.
    """
    return _has_true_label(agent_name, "is_primary")
