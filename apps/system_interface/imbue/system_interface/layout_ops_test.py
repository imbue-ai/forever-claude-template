"""Tests for ``layout_ops`` (mutex, inspect serializer, list helper, op-name validation)."""

import json
from pathlib import Path
from typing import Any

from imbue.system_interface.layout_ops import LayoutMutex
from imbue.system_interface.layout_ops import is_broadcasting_op
from imbue.system_interface.layout_ops import is_known_op
from imbue.system_interface.layout_ops import is_mutating_op
from imbue.system_interface.layout_ops import layout_inspect
from imbue.system_interface.layout_ops import layout_list
from imbue.mngr.utils.polling import wait_for


def test_known_ops_cover_the_full_surface() -> None:
    for op in (
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
    ):
        assert is_known_op(op), op


def test_unknown_op_is_rejected() -> None:
    assert not is_known_op("explode")
    assert not is_known_op("")


def test_mutating_ops_include_focus_but_not_refresh_or_queries() -> None:
    """``focus`` mutates serialized active-panel state, so it acquires the mutex."""
    assert is_mutating_op("focus")
    # Pure queries bypass.
    assert not is_mutating_op("list")
    assert not is_mutating_op("inspect")
    # ``refresh`` is state-preserving and bypasses too.
    assert not is_mutating_op("refresh")


def test_broadcasting_ops_exclude_list_and_inspect() -> None:
    """``list`` and ``inspect`` are pure server-side queries; they never broadcast."""
    assert not is_broadcasting_op("list")
    assert not is_broadcasting_op("inspect")
    assert is_broadcasting_op("refresh")
    assert is_broadcasting_op("open")


def test_mutex_acquire_succeeds_when_free() -> None:
    mutex = LayoutMutex(ttl_seconds=0.5)
    assert mutex.try_acquire("agent-a", "move", {"ref": "service:web"}) is None


def test_mutex_acquire_fails_while_held_and_returns_holder_info() -> None:
    mutex = LayoutMutex(ttl_seconds=1.0)
    assert mutex.try_acquire("agent-a", "move", {"ref": "service:web"}) is None

    holder = mutex.try_acquire("agent-b", "split", {"ref": "service:api"})
    assert holder is not None
    assert holder["agent_id"] == "agent-a"
    assert holder["operation"] == "move"
    assert holder["args"] == {"ref": "service:web"}
    assert isinstance(holder["started_at"], float)


def test_mutex_release_allows_subsequent_acquire() -> None:
    mutex = LayoutMutex(ttl_seconds=1.0)
    assert mutex.try_acquire("agent-a", "move", {}) is None
    mutex.release("agent-a", "move")
    assert mutex.try_acquire("agent-b", "split", {}) is None


def test_mutex_release_of_non_holder_is_noop() -> None:
    """A stale release from a previous holder must NOT release a current holder's slot."""
    mutex = LayoutMutex(ttl_seconds=1.0)
    assert mutex.try_acquire("agent-a", "move", {}) is None
    # Stale release from a different agent.
    mutex.release("agent-b", "move")
    holder = mutex.try_acquire("agent-c", "split", {})
    assert holder is not None
    assert holder["agent_id"] == "agent-a"


def test_mutex_auto_releases_after_ttl() -> None:
    mutex = LayoutMutex(ttl_seconds=0.01)
    assert mutex.try_acquire("agent-a", "move", {}) is None
    wait_for(
        lambda: mutex.try_acquire("agent-b", "split", {}) is None,
        timeout=1.0,
        poll_interval=0.005,
        error_message="mutex did not auto-release after TTL",
    )


def _write_layout(path: Path, dockview: dict[str, Any], panel_params: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"dockview": dockview, "panelParams": panel_params}))


def test_inspect_returns_empty_when_no_layout_file(tmp_path: Path) -> None:
    summary = layout_inspect(tmp_path / "missing.json", {})
    assert summary == {"active_panel": None, "panels": [], "tree": None}


def test_inspect_resolves_chat_ref_via_agent_name_map(tmp_path: Path) -> None:
    layout_path = tmp_path / "layout.json"
    _write_layout(
        layout_path,
        dockview={
            "panels": {"p1": {"id": "p1", "title": "alice-chat"}},
            "grid": {"root": {"type": "leaf", "data": {"views": ["p1"], "activeView": "p1", "size": 1.0}}},
        },
        panel_params={"p1": {"panelType": "chat", "chatAgentId": "agent-42"}},
    )
    summary = layout_inspect(layout_path, {"agent-42": "alice"})
    refs = [p["ref"] for p in summary["panels"]]
    assert "chat:alice" in refs


def test_inspect_resolves_iframe_with_service_name(tmp_path: Path) -> None:
    layout_path = tmp_path / "layout.json"
    _write_layout(
        layout_path,
        dockview={
            "panels": {"p1": {"id": "p1", "title": "web"}},
            "grid": {"root": {"type": "leaf", "data": {"views": ["p1"], "activeView": "p1", "size": 1.0}}},
        },
        panel_params={"p1": {"panelType": "iframe", "serviceName": "web"}},
    )
    summary = layout_inspect(layout_path, {})
    refs = [p["ref"] for p in summary["panels"]]
    assert "service:web" in refs


def test_inspect_emits_url_ref_for_ad_hoc_iframe(tmp_path: Path) -> None:
    layout_path = tmp_path / "layout.json"
    _write_layout(
        layout_path,
        dockview={
            "panels": {"p1": {"id": "p1", "title": "external"}},
            "grid": {"root": {"type": "leaf", "data": {"views": ["p1"], "activeView": "p1", "size": 1.0}}},
        },
        panel_params={"p1": {"panelType": "iframe", "url": "https://example.com/"}},
    )
    summary = layout_inspect(layout_path, {})
    ref = summary["panels"][0]["ref"]
    assert ref.startswith("url:")


def test_inspect_preserves_grid_orientation_in_tree(tmp_path: Path) -> None:
    layout_path = tmp_path / "layout.json"
    _write_layout(
        layout_path,
        dockview={
            "panels": {
                "p1": {"id": "p1", "title": "chat"},
                "p2": {"id": "p2", "title": "web"},
            },
            "grid": {
                "root": {
                    "type": "branch",
                    "orientation": "HORIZONTAL",
                    "size": 1.0,
                    "data": [
                        {"type": "leaf", "data": {"views": ["p1"], "activeView": "p1", "size": 0.4}},
                        {"type": "leaf", "data": {"views": ["p2"], "activeView": "p2", "size": 0.6}},
                    ],
                }
            },
        },
        panel_params={
            "p1": {"panelType": "chat", "chatAgentId": "agent-42"},
            "p2": {"panelType": "iframe", "serviceName": "web"},
        },
    )
    summary = layout_inspect(layout_path, {"agent-42": "alice"})
    tree = summary["tree"]
    assert tree["type"] == "branch"
    assert tree["orientation"] == "horizontal"
    assert len(tree["children"]) == 2
    # Coarse size ratios are preserved so an agent can reason about layout.
    assert tree["children"][0]["size_ratio"] == 0.4
    assert tree["children"][1]["size_ratio"] == 0.6


def test_list_marks_open_services_via_layout_json(tmp_path: Path) -> None:
    layout_path = tmp_path / "layout.json"
    _write_layout(
        layout_path,
        dockview={"panels": {"p1": {"id": "p1", "title": "web"}}},
        panel_params={"p1": {"panelType": "iframe", "serviceName": "web"}},
    )
    entries = layout_list(
        service_names=("web", "api"),
        agents=[],
        layout_json_path=layout_path,
        agent_name_by_id={},
    )
    by_ref = {e["ref"]: e for e in entries}
    assert by_ref["service:web"]["is_open"] is True
    assert by_ref["service:api"]["is_open"] is False


def test_list_hides_reserved_system_interface_entry(tmp_path: Path) -> None:
    """The chrome's own ``system_interface`` registration is filtered server-side
    so every caller (script, direct HTTP, future SDKs) sees the same set."""
    entries = layout_list(
        service_names=("web", "system_interface", "api"),
        agents=[],
        layout_json_path=tmp_path / "missing.json",
        agent_name_by_id={},
    )
    refs = {e["ref"] for e in entries}
    assert "service:web" in refs
    assert "service:api" in refs
    assert "service:system_interface" not in refs


def test_list_marks_running_agents(tmp_path: Path) -> None:
    entries = layout_list(
        service_names=(),
        agents=[
            {"id": "a1", "name": "alice", "state": "running", "labels": {}, "work_dir": None},
            {"id": "a2", "name": "bob", "state": "stopped", "labels": {}, "work_dir": None},
        ],
        layout_json_path=tmp_path / "missing.json",
        agent_name_by_id={"a1": "alice", "a2": "bob"},
    )
    by_ref = {e["ref"]: e for e in entries}
    assert by_ref["chat:alice"]["is_running"] is True
    assert by_ref["chat:bob"]["is_running"] is False
