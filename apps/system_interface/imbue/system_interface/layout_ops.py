"""Server-side support for the agent-driven layout-mutation surface.

The agent-facing helper (``scripts/layout.py``) talks to a single loopback
endpoint, ``POST /api/layout/broadcast``, which dispatches to handlers in
this module:

- ``layout_inspect``: read the persisted ``layout.json`` and produce a
  ref-resolved tree describing the live dockview state.
- ``layout_list``: enumerate every addressable thing in the workspace
  (registered services + mngr-level agents) with open/running flags.
- ``LayoutMutex``: in-process advisory mutex with a fixed TTL window;
  mutating ops acquire before broadcasting. Conflicting attempts get an
  HTTP 409 with the holder's metadata so they can decide whether to retry.

Read-only ops (``inspect``, ``list``, ``refresh``, ``reload_system_interface``)
bypass the mutex. ``focus`` acquires it because it mutates the serialized
active-panel state.
"""

import hashlib
import json
import threading
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

from loguru import logger as _loguru_logger

# Path prefix the dispatcher uses for the workspace terminal service. The
# agent-attached terminal URL the frontend stores is
# ``/service/terminal/?arg=_&arg=agent&arg=<name>``; the anonymous "New
# terminal" path uses ``arg=workdir`` instead and is left as ``terminal:<hash>``.
_TERMINAL_SERVICE_URL_PATH = "/service/terminal/"

# Set of op names the endpoint dispatches on. Anything else is a 400.
_KNOWN_OPS: frozenset[str] = frozenset(
    {
        "list",
        "inspect",
        "open",
        "focus",
        "split",
        "close",
        "move",
        "rename",
        "maximize",
        "restore",
        "replace-url",
        "refresh",
        "reload_system_interface",
    }
)

# Ops that mutate serialized layout state and therefore acquire the mutex.
# ``focus`` is here because dockview persists ``activeView``/``activeGroup``,
# so swapping focus changes the next autosave's bytes.
_MUTATING_OPS: frozenset[str] = frozenset(
    {
        "open",
        "focus",
        "split",
        "close",
        "move",
        "rename",
        "maximize",
        "restore",
        "replace-url",
    }
)

# Ops that broadcast a layout_op message to the frontend. ``list`` and
# ``inspect`` are pure queries that read disk directly without involving
# the frontend.
_BROADCASTING_OPS: frozenset[str] = frozenset(
    {
        "open",
        "focus",
        "split",
        "close",
        "move",
        "rename",
        "maximize",
        "restore",
        "replace-url",
        "refresh",
        "reload_system_interface",
    }
)

# The mutex TTL. Picked to be comfortably longer than a single dockview
# mutation round-trip (typically <50 ms) while still small enough that a
# wedged op can't lock the workspace for an annoying length of time.
_MUTEX_TTL_SECONDS: float = 0.5

# Reserved service entries that aren't user-facing tabs. Filtered out of
# ``layout_list`` so every caller (script, direct HTTP, future SDKs) gets
# the same view of "addressable things" without duplicating the filter.
_HIDDEN_SERVICES: frozenset[str] = frozenset({"system_interface"})


def is_known_op(op: str) -> bool:
    return op in _KNOWN_OPS


def is_mutating_op(op: str) -> bool:
    return op in _MUTATING_OPS


def is_broadcasting_op(op: str) -> bool:
    return op in _BROADCASTING_OPS


class LayoutMutex:
    """Advisory in-process mutex protecting layout-mutating ops.

    Acquisition is non-blocking and TTL-bounded: a holder that doesn't
    explicitly release is auto-released after ``_MUTEX_TTL_SECONDS``. The
    server never blocks on a busy mutex -- conflicting requests fail
    immediately with HTTP 409 and a description of the in-flight op so the
    caller can pick its own retry strategy.
    """

    def __init__(self, ttl_seconds: float = _MUTEX_TTL_SECONDS) -> None:
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._holder: dict[str, Any] | None = None

    def try_acquire(self, agent_id: str, op: str, args: dict[str, Any]) -> dict[str, Any] | None:
        """Attempt to take the mutex.

        Returns ``None`` on success. On contention, returns a dict
        describing the holder so the caller can emit an HTTP 409.
        """
        now = time.monotonic()
        with self._lock:
            if self._holder is not None:
                holder_age = now - self._holder["started_at_monotonic"]
                if holder_age < self._ttl_seconds:
                    return {
                        "agent_id": self._holder["agent_id"],
                        "operation": self._holder["op"],
                        "args": self._holder["args"],
                        "started_at": self._holder["started_at_wall"],
                    }
            self._holder = {
                "agent_id": agent_id,
                "op": op,
                "args": args,
                "started_at_monotonic": now,
                "started_at_wall": time.time(),
            }
            return None

    def release(self, agent_id: str, op: str) -> None:
        """Best-effort release. Silent no-op if the slot was already reused.

        The mutex is advisory and TTL-bounded, so a concurrent client may
        have stolen the slot after our TTL expired. Don't error on that.
        """
        with self._lock:
            holder = self._holder
            if holder is not None and holder["agent_id"] == agent_id and holder["op"] == op:
                self._holder = None

    def retry_after_ms(self) -> int:
        """How long the caller should wait before retrying, in milliseconds."""
        return int(self._ttl_seconds * 1000)


def _short_hash(panel_id: str) -> str:
    """Stable opaque-but-readable short id derived from a dockview panel id.

    Used as the suffix for ad-hoc panel refs (``terminal:<hash>`` /
    ``url:<hash>``) so they don't renumber when other panels close. Eight
    hex chars is plenty -- collisions between coexisting panels are
    astronomically unlikely.
    """
    return hashlib.sha256(panel_id.encode("utf-8")).hexdigest()[:8]


def allocate_terminal_panel_id() -> tuple[str, str]:
    """Allocate a fresh panel id + ``terminal:<hash>`` ref for terminal creation.

    Returned by the broadcast endpoint when the agent runs
    ``layout.py open terminal`` / ``layout.py split terminal``: the server
    pre-commits the panel id so the HTTP response can carry the ref the
    frontend will ultimately give the new tab, and the frontend uses the
    supplied id verbatim instead of generating its own. This is the only
    creation path where the script returns a ref synchronously -- every
    other ref kind either dedups against the existing panel set or is
    discoverable via a subsequent ``inspect``.
    """
    panel_id = f"iframe-terminal-{uuid.uuid4().hex}"
    return panel_id, f"terminal:{_short_hash(panel_id)}"


def _extract_agent_terminal_name(url: str) -> str | None:
    """If ``url`` is the per-agent terminal URL, return the bound agent name.

    The frontend's chat-panel "Open agent terminal" button mints iframes
    pointed at ``/service/terminal/?arg=_&arg=agent&arg=<name>`` (the
    ttyd dispatch script attaches to the named tmux session). Detecting
    this shape lets ``_resolve_ref`` project these panels as
    ``chat-terminal:<name>`` -- a stable, predictable ref that mirrors
    the ``chat:<name>`` convention -- instead of the opaque
    ``terminal:<hash>`` it would otherwise emit. Anonymous terminals
    minted via the "New terminal" button use ``arg=workdir`` instead and
    fall through to the ``terminal:<hash>`` branch.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.path != _TERMINAL_SERVICE_URL_PATH:
        return None
    # ``parse_qs`` returns repeated-key values in the order they appear in
    # the query string, which is what the frontend's URL builder emits:
    # ``arg=_&arg=agent&arg=<name>``.
    args = urllib.parse.parse_qs(parsed.query, keep_blank_values=True).get("arg", [])
    if len(args) != 3 or args[0] != "_" or args[1] != "agent":
        return None
    name = args[2]
    return name or None


def _resolve_ref(
    panel_id: str,
    params: dict[str, Any] | None,
    agent_name_by_id: dict[str, str],
) -> dict[str, Any]:
    """Build a stable type-prefixed ref + descriptive fields for a panel.

    ``params`` is the entry from the persisted ``panelParams`` map. When
    ``panelType`` is missing the panel falls back to a ``url:`` ref
    derived from the panel id.
    """
    params = params or {}
    panel_type = params.get("panelType")
    service_name = params.get("serviceName")
    chat_agent_id = params.get("chatAgentId")
    subagent_session_id = params.get("subagentSessionId")
    url = params.get("url")
    if panel_type == "chat":
        # Prefer the live agent name; fall back to the agent id if we
        # can't resolve it (eg. a chat for an agent that no longer exists).
        # If neither is available (eg. corrupt panelParams missing
        # chatAgentId), fall back to a panel-id-derived short hash so the
        # ref remains addressable instead of degrading to a bare ``chat:``.
        agent_name = agent_name_by_id.get(chat_agent_id or "", chat_agent_id or "")
        ref = f"chat:{agent_name}" if agent_name else f"url:{_short_hash(panel_id)}"
    elif panel_type == "subagent":
        ref = f"subagent:{subagent_session_id or _short_hash(panel_id)}"
    elif panel_type == "iframe" and service_name:
        ref = f"service:{service_name}"
    elif panel_type == "iframe" and isinstance(url, str) and (agent_terminal_name := _extract_agent_terminal_name(url)) is not None:
        # Per-agent terminals get the symmetric ``chat-terminal:<name>``
        # form so they're addressable by name (parallel to ``chat:<name>``)
        # rather than only via the opaque ``terminal:<hash>``.
        ref = f"chat-terminal:{agent_terminal_name}"
    elif panel_type == "iframe" and isinstance(url, str) and url.startswith(_TERMINAL_SERVICE_URL_PATH):
        ref = f"terminal:{_short_hash(panel_id)}"
    elif panel_type == "iframe":
        ref = f"url:{_short_hash(panel_id)}"
    else:
        ref = f"url:{_short_hash(panel_id)}"
    summary: dict[str, Any] = {
        "ref": ref,
        "panel_id": panel_id,
        "panel_type": panel_type,
        "service_name": service_name,
        "title": params.get("title"),
    }
    # Surface the iframe URL so the CLI's wait-stable poll for
    # ``replace-url`` has a field to compare against. Only set for iframe
    # panels (chat/subagent have no URL); kept off when missing so the
    # absent-vs-empty distinction is clear in JSON output.
    if panel_type == "iframe" and isinstance(url, str):
        summary["url"] = url
    return summary


def _orthogonal_orientation(orientation: str) -> str:
    """Flip a dockview ``Orientation`` string between ``HORIZONTAL`` and ``VERTICAL``.

    Mirrors dockview-core's ``orthogonal`` helper: gridview alternates the
    effective orientation of branch nodes at each level of nesting, but the
    persisted ``layout.json`` only stores the *root* grid orientation --
    individual branch nodes carry no ``orientation`` field. So whenever we
    recurse into a child branch we flip the orientation we were called
    with. Anything other than the two known values flips to ``HORIZONTAL``
    so the renderer can still produce something deterministic on a
    malformed input.
    """
    return "VERTICAL" if orientation == "HORIZONTAL" else "HORIZONTAL"


def _serialize_grid_node(
    node: dict[str, Any],
    panel_summaries: dict[str, dict[str, Any]],
    panels_meta: dict[str, dict[str, Any]],
    orientation: str,
) -> dict[str, Any]:
    """Recursively project the dockview grid tree into a compact summary.

    ``panel_summaries`` maps panel_id -> the resolved ref dict from
    ``_resolve_ref``. ``panels_meta`` maps panel_id -> the dockview-panel
    record (for the title fallback when params.title is unset).
    ``orientation`` is the effective dockview orientation for the current
    branch level, threaded through from ``layout_inspect`` because the
    persisted JSON only stores the root grid orientation -- the gridview
    contract is that nested branches always alternate.
    """
    node_type = node.get("type")
    if node_type == "leaf":
        data = node.get("data", {}) or {}
        view_ids = list(data.get("views", []) or [])
        active_view = data.get("activeView")
        panels: list[dict[str, Any]] = []
        for panel_id in view_ids:
            summary = dict(panel_summaries.get(panel_id, {"ref": f"url:{_short_hash(panel_id)}", "panel_id": panel_id}))
            if not summary.get("title"):
                meta = panels_meta.get(panel_id, {})
                summary["title"] = meta.get("title")
            summary["active"] = panel_id == active_view
            panels.append(summary)
        return {
            "type": "leaf",
            "size_ratio": data.get("size"),
            "panels": panels,
        }
    # Branch node -- ``arrangement`` describes how children are laid out:
    # ``row`` = children side by side (dockview internal ``HORIZONTAL``
    # divider), ``column`` = children stacked top to bottom (internal
    # ``VERTICAL`` divider). The names match how panels are arranged on
    # screen rather than the divider's orientation.
    data = node.get("data", []) or []
    child_orientation = _orthogonal_orientation(orientation)
    return {
        "type": "branch",
        "arrangement": "row" if orientation == "HORIZONTAL" else "column",
        "size_ratio": node.get("size"),
        "children": [
            _serialize_grid_node(child, panel_summaries, panels_meta, child_orientation) for child in data
        ],
    }


def _read_layout(layout_json_path: Path | None) -> dict[str, Any] | None:
    """Read and JSON-decode the persisted ``layout.json``, or return None.

    Centralizes the file-existence + decode + error-log path so callers
    (``layout_inspect`` / ``_collect_open_refs``) can share it without
    drifting on which exceptions are caught or how they're logged.
    """
    if layout_json_path is None or not layout_json_path.exists():
        return None
    try:
        return json.loads(layout_json_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        _loguru_logger.opt(exception=e).warning("Failed to read layout.json at {}", layout_json_path)
        return None


def _build_panel_summaries(
    raw: dict[str, Any], agent_name_by_id: dict[str, str]
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Build the panel_id -> ref summary map (with title fallback applied).

    Returns ``(summaries, panels_meta)`` so callers that also want the raw
    ``panels`` block (for the tree serializer) can use it directly.
    """
    panel_params = raw.get("panelParams", {}) or {}
    panels_meta = (raw.get("dockview", {}) or {}).get("panels", {}) or {}
    summaries: dict[str, dict[str, Any]] = {}
    for panel_id in panels_meta.keys():
        summary = _resolve_ref(panel_id, panel_params.get(panel_id), agent_name_by_id)
        if not summary.get("title"):
            summary["title"] = panels_meta.get(panel_id, {}).get("title")
        summaries[panel_id] = summary
    return summaries, panels_meta


def layout_inspect(layout_json_path: Path | None, agent_name_by_id: dict[str, str]) -> dict[str, Any]:
    """Read the persisted ``layout.json`` and produce a ref-resolved summary.

    The frontend autosaves layout state with a 1.5 s debounce, so this is
    correct modulo that staleness window. ``layout_json_path`` is None when
    the workspace_server has no primary agent configured (dev/test setups);
    if the file is missing or unreadable for any reason, returns an empty
    layout (``{"panels": []}``) -- which the agent can interpret as "no UI
    initialized yet" without erroring.
    """
    raw = _read_layout(layout_json_path)
    if raw is None:
        return {"active_panel": None, "panels": [], "tree": None}
    panel_summaries, panels_meta = _build_panel_summaries(raw, agent_name_by_id)
    flat_panels: list[dict[str, Any]] = [
        {k: v for k, v in summary.items() if k != "panel_id"} for summary in panel_summaries.values()
    ]
    dockview = raw.get("dockview", {}) or {}
    grid = dockview.get("grid", {}) or {}
    root = grid.get("root")
    # The root branch carries no ``orientation`` field of its own; dockview
    # stores it once at the grid level and the gridview contract flips it
    # at each nested branch. Default to ``HORIZONTAL`` when missing so a
    # malformed / empty layout still renders deterministically.
    root_orientation = grid.get("orientation") or "HORIZONTAL"
    tree = _serialize_grid_node(root, panel_summaries, panels_meta, root_orientation) if root else None
    return {
        "active_panel": dockview.get("activeGroup"),
        "panels": flat_panels,
        "tree": tree,
    }


def layout_list(
    service_names: tuple[str, ...],
    agents: list[dict[str, Any]],
    layout_json_path: Path | None,
    agent_name_by_id: dict[str, str],
) -> list[dict[str, Any]]:
    """Enumerate everything addressable in the workspace.

    Each entry: ``{ref, kind, display_name, is_open, is_running}``.
    ``kind`` is one of ``service`` / ``agent`` / ``agent-terminal``. Every
    agent yields both a ``chat:<name>`` (``agent``) entry and its
    separately-addressable ``chat-terminal:<name>`` (``agent-terminal``)
    entry.
    """
    open_refs = _collect_open_refs(layout_json_path, agent_name_by_id)
    entries: list[dict[str, Any]] = []
    for service_name in service_names:
        if service_name in _HIDDEN_SERVICES:
            continue
        ref = f"service:{service_name}"
        entries.append(
            {
                "ref": ref,
                "kind": "service",
                "display_name": service_name,
                "is_open": ref in open_refs,
                "is_running": True,
            }
        )
    for agent in agents:
        name = agent.get("name") or agent.get("id") or ""
        ref = f"chat:{name}"
        state = agent.get("state", "")
        # ``state`` strings vary across providers but ``running`` is the
        # conventional alive value used by mngr observe.
        is_running = state == "running"
        entries.append(
            {
                "ref": ref,
                "kind": "agent",
                "display_name": name,
                "is_open": ref in open_refs,
                "is_running": is_running,
            }
        )
        # The agent-attached terminal is a separately-addressable singleton
        # (one tmux session per agent name). Its ``is_open`` reflects
        # whether a panel pointed at ``/service/terminal/?arg=_&arg=agent
        # &arg=<name>`` is currently mounted; ``is_running`` mirrors the
        # owning agent so a stopped agent's terminal is flagged as such.
        terminal_ref = f"chat-terminal:{name}"
        entries.append(
            {
                "ref": terminal_ref,
                "kind": "agent-terminal",
                "display_name": f"{name} terminal",
                "is_open": terminal_ref in open_refs,
                "is_running": is_running,
            }
        )
    return entries


def _collect_open_refs(layout_json_path: Path | None, agent_name_by_id: dict[str, str]) -> set[str]:
    """Return the set of refs currently mounted in the saved layout."""
    raw = _read_layout(layout_json_path)
    if raw is None:
        return set()
    panel_summaries, _ = _build_panel_summaries(raw, agent_name_by_id)
    return {summary["ref"] for summary in panel_summaries.values()}
