"""Tests for the agent-facing layout.py helper.

These tests exercise the behavior an agent depends on:

- ``list`` and ``inspect`` post to the unified loopback endpoint, filter
  reserved chrome services from ``list``, and emit YAML by default with
  ``--json`` as the escape hatch.
- ``open`` waits for service registration before posting and uses the
  ``service:`` ref shorthand.
- ``split`` / ``move`` enforce the direction enum and pass the
  ``--relative-to`` ref through.
- ``replace-url`` rejects URLs that aren't ``service:<name>...`` or
  ``https://...``.
- Each transport status (200/400/404/409/network) maps to a distinct
  exit code.
- The ``X-Mngr-Agent-Id`` header rides every request.
"""

from __future__ import annotations

import importlib.util
import json
import urllib.request
from pathlib import Path
from typing import Any

import pytest
import tomlkit

_SCRIPT = Path(__file__).parent / "layout.py"
_spec = importlib.util.spec_from_file_location("layout", _SCRIPT)
assert _spec is not None and _spec.loader is not None
layout = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(layout)


def _write_apps_toml(path: Path, names: list[str]) -> None:
    doc = tomlkit.document()
    apps = tomlkit.aot()
    for name in names:
        entry = tomlkit.table()
        entry["name"] = name
        entry["url"] = f"http://localhost:9000/{name}"
        apps.append(entry)
    doc["applications"] = apps
    path.write_text(tomlkit.dumps(doc))


def _make_fake_post(
    posted: list[tuple[str, dict[str, Any]]],
    response: tuple[int, dict[str, Any] | str] = (200, {"ok": True}),
):
    def fake_post(op: str, args: dict[str, Any]) -> tuple[int, dict[str, Any] | str]:
        posted.append((op, args))
        return response

    return fake_post


def test_list_emits_server_entries_as_yaml(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``list`` is a thin pass-through: the server (layout_ops.layout_list)
    is the single source of truth for which entries are user-facing, and
    the script prints whatever the server returns."""
    posted: list[tuple[str, dict[str, Any]]] = []
    entries = [
        {"ref": "service:web", "kind": "service", "display_name": "web", "is_open": True, "is_running": True},
        {"ref": "chat:alice", "kind": "agent", "display_name": "alice", "is_open": False, "is_running": True},
    ]
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted, (200, {"ok": True, "entries": entries})))

    rc = layout.main(["list"])
    assert rc == 0
    assert posted == [("list", {})]
    out = capsys.readouterr().out
    assert "service:web" in out
    assert "chat:alice" in out


def test_list_json_emits_structured_json(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    posted: list[tuple[str, dict[str, Any]]] = []
    entries = [
        {"ref": "service:web", "kind": "service", "display_name": "web", "is_open": True, "is_running": True},
    ]
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted, (200, {"ok": True, "entries": entries})))

    rc = layout.main(["list", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed == entries


def test_inspect_emits_layout_payload(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    posted: list[tuple[str, dict[str, Any]]] = []
    layout_obj = {"panels": [{"ref": "chat:alice"}], "tree": None}
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted, (200, {"ok": True, "layout": layout_obj})))

    rc = layout.main(["inspect", "--json"])
    assert rc == 0
    assert posted == [("inspect", {})]
    assert json.loads(capsys.readouterr().out) == layout_obj


def test_open_waits_for_registration_then_posts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    apps_file = tmp_path / "applications.toml"
    _write_apps_toml(apps_file, ["web"])
    monkeypatch.setenv(layout.ENV_APPLICATIONS_FILE, str(apps_file))

    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))

    rc = layout.main(["open", "web"])
    assert rc == 0
    assert posted == [("open", {"ref": "service:web", "new_group": False})]


def test_open_fails_when_service_not_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    apps_file = tmp_path / "applications.toml"
    _write_apps_toml(apps_file, ["other"])
    monkeypatch.setenv(layout.ENV_APPLICATIONS_FILE, str(apps_file))
    monkeypatch.setattr(layout, "_REGISTRATION_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(layout, "_REGISTRATION_POLL_INTERVAL_SECONDS", 0.01)

    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))

    rc = layout.main(["open", "web"])
    assert rc == layout.EXIT_NOT_REGISTERED
    assert posted == []
    err = capsys.readouterr().err
    assert "not registered" in err


def test_open_full_ref_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    apps_file = tmp_path / "applications.toml"
    _write_apps_toml(apps_file, ["web"])
    monkeypatch.setenv(layout.ENV_APPLICATIONS_FILE, str(apps_file))
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))

    rc = layout.main(["open", "service:web"])
    assert rc == 0
    assert posted == [("open", {"ref": "service:web", "new_group": False})]


def test_open_new_group_flag_sets_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--new-group`` opts out of the share-existing-group default."""
    apps_file = tmp_path / "applications.toml"
    _write_apps_toml(apps_file, ["web"])
    monkeypatch.setenv(layout.ENV_APPLICATIONS_FILE, str(apps_file))
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))

    rc = layout.main(["open", "service:web", "--new-group"])
    assert rc == 0
    assert posted == [("open", {"ref": "service:web", "new_group": True})]


def test_split_passes_relative_to_and_direction(monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))
    # Bypass the registration wait for this synthetic non-service ref.
    monkeypatch.setattr(layout, "_wait_for_registration", lambda *a, **kw: True)

    rc = layout.main(["split", "url:abc12345", "--relative-to", "chat:alice", "--direction", "above"])
    assert rc == 0
    op, args = posted[0]
    assert op == "split"
    assert args == {
        "ref": "url:abc12345",
        "relative_to": "chat:alice",
        "direction": "above",
        "ratio": 0.6,
        "new_group": False,
    }


def test_split_new_group_flag_sets_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """``split --new-group`` flips the new_group payload field on."""
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))
    monkeypatch.setattr(layout, "_wait_for_registration", lambda *a, **kw: True)

    rc = layout.main(["split", "service:web", "--relative-to", "chat:alice", "--new-group"])
    assert rc == 0
    op, args = posted[0]
    assert op == "split"
    assert args["new_group"] is True


def test_move_new_group_flag_sets_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """``move --new-group`` flips the new_group payload field on."""
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))

    rc = layout.main(
        ["move", "service:web", "--relative-to", "chat:alice", "--direction", "right", "--new-group"]
    )
    assert rc == 0
    op, args = posted[0]
    assert op == "move"
    assert args["new_group"] is True


def test_split_preserves_self_in_relative_to(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--relative-to self`` is the documented default and must reach the server verbatim."""
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))
    monkeypatch.setattr(layout, "_wait_for_registration", lambda *a, **kw: True)

    rc = layout.main(["split", "service:web", "--relative-to", "self"])
    assert rc == 0
    op, args = posted[0]
    assert op == "split"
    assert args["relative_to"] == "self"


def test_split_normalizes_bare_service_in_relative_to(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--relative-to web`` (bare service name) must be expanded to ``service:web``."""
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))
    monkeypatch.setattr(layout, "_wait_for_registration", lambda *a, **kw: True)

    rc = layout.main(["split", "service:api", "--relative-to", "web"])
    assert rc == 0
    op, args = posted[0]
    assert op == "split"
    assert args["relative_to"] == "service:web"


def test_move_preserves_self_in_relative_to(monkeypatch: pytest.MonkeyPatch) -> None:
    """``move --relative-to self`` must NOT get rewritten to ``service:self``."""
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))

    rc = layout.main(["move", "service:web", "--relative-to", "self", "--direction", "right"])
    assert rc == 0
    op, args = posted[0]
    assert op == "move"
    assert args["relative_to"] == "self"


def test_move_requires_known_direction(monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))

    with pytest.raises(SystemExit):
        layout.main(["move", "service:web", "--relative-to", "chat:alice", "--direction", "diagonal"])
    assert posted == []


def test_replace_url_rejects_non_service_non_https(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))

    with pytest.raises(SystemExit) as exc_info:
        layout.main(["replace-url", "service:web", "http://insecure.local/"])
    assert exc_info.value.code == layout.EXIT_BAD_REQUEST
    assert posted == []
    err = capsys.readouterr().err
    assert "service:<name>" in err or "https://" in err


def test_replace_url_accepts_service_shorthand(monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))

    rc = layout.main(["replace-url", "service:web", "service:api/health"])
    assert rc == 0
    op, args = posted[0]
    assert op == "replace-url"
    assert args == {"ref": "service:web", "url": "service:api/health"}


def test_refresh_posts_ref_with_service_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))

    rc = layout.main(["refresh", "web"])
    assert rc == 0
    assert posted == [("refresh", {"ref": "service:web"})]


def test_close_normalizes_bare_service_shorthand(monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))

    rc = layout.main(["close", "web"])
    assert rc == 0
    assert posted == [("close", {"ref": "service:web"})]


def test_network_failure_returns_distinct_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(layout, "_post_layout", lambda op, args: (-1, "Connection refused"))
    rc = layout.main(["refresh", "web"])
    assert rc == layout.EXIT_NETWORK


def test_conflict_returns_distinct_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    body = {
        "detail": "Another layout op is in flight",
        "retry_after_ms": 500,
        "in_flight": {"agent_id": "other-agent", "operation": "move", "args": {}, "started_at": 1700000000.0},
    }
    monkeypatch.setattr(layout, "_post_layout", lambda op, args: (409, body))
    rc = layout.main(["focus", "service:web"])
    assert rc == layout.EXIT_CONFLICT
    err = capsys.readouterr().err
    assert "agent_id=other-agent" in err
    assert "op=move" in err


def test_not_found_returns_distinct_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(layout, "_post_layout", lambda op, args: (404, {"detail": "unknown ref"}))
    rc = layout.main(["focus", "service:nonexistent"])
    assert rc == layout.EXIT_NOT_FOUND


def test_bad_request_returns_distinct_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(layout, "_post_layout", lambda op, args: (400, {"detail": "bad arg"}))
    rc = layout.main(["close", "service:web"])
    assert rc == layout.EXIT_BAD_REQUEST


def test_post_layout_sends_agent_id_header_and_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end check that _post_layout emits the right URL, headers, and body shape."""
    monkeypatch.setenv(layout.ENV_MNGR_AGENT_ID, "agent-42")
    monkeypatch.setenv(layout.ENV_WORKSPACE_URL, "http://127.0.0.1:8000")

    captured: dict[str, Any] = {}

    class _FakeResponse:
        status = 200

        def __init__(self, text: str) -> None:
            self._text = text

        def read(self) -> bytes:
            return self._text.encode("utf-8")

        def __enter__(self) -> _FakeResponse:
            return self

        def __exit__(self, *_: object) -> None:
            return None

    def fake_urlopen(req: urllib.request.Request, timeout: float) -> _FakeResponse:
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["body"] = req.data
        return _FakeResponse('{"ok": true}')

    monkeypatch.setattr(layout.urllib.request, "urlopen", fake_urlopen)

    status, body = layout._post_layout("focus", {"ref": "service:web"})
    assert status == 200
    assert body == {"ok": True}
    assert captured["url"] == "http://127.0.0.1:8000/api/layout/broadcast"
    # urllib normalizes header names to title-case in header_items().
    header_names = {k.lower(): v for k, v in captured["headers"].items()}
    assert header_names.get("x-mngr-agent-id") == "agent-42"
    parsed_body = json.loads(captured["body"].decode("utf-8"))
    assert parsed_body == {"op": "focus", "args": {"ref": "service:web"}, "agent_id": "agent-42"}
