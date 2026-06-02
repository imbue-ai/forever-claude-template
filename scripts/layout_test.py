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


@pytest.fixture(autouse=True)
def _skip_wait_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass the wait-stable poll for tests that assert on broadcast args.

    Mutating ops in production block until the post-op layout state is
    observable via ``inspect``; the tests in this file mock ``_post_layout``
    and assert on exact broadcast args, which the extra ``inspect`` calls
    from wait-stable would distort. The CLI's contract for this env var is
    documented in ``scripts/layout.py``. Tests that *want* to exercise the
    wait-stable behavior explicitly remove this env var via monkeypatch.
    """
    monkeypatch.setenv(layout.ENV_NO_WAIT_STABLE, "1")


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
    assert rc == layout.EXIT_ERROR
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


def test_open_external_url_skips_registration_and_posts_bare_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare ``https://`` target is an external-URL ref: it must NOT be
    treated as a service name (no applications.toml registration check)
    and reaches the server verbatim."""
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))
    # No applications.toml set up and no _wait_for_registration override:
    # if the URL were misclassified as a service this would fail/hang.

    rc = layout.main(["open", "https://example.com/dashboard"])
    assert rc == 0
    assert posted == [("open", {"ref": "https://example.com/dashboard", "new_group": False})]


def test_open_terminal_prints_returned_ref_to_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``open terminal`` is the one creation path the server resolves
    synchronously: the broadcast endpoint pre-allocates the panel id and
    returns ``terminal:<hash>`` in the HTTP response so the script can
    print it. The agent then has a stable handle for follow-up ops
    without round-tripping through ``inspect``."""
    apps_file = tmp_path / "applications.toml"
    _write_apps_toml(apps_file, ["terminal"])
    monkeypatch.setenv(layout.ENV_APPLICATIONS_FILE, str(apps_file))

    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        layout, "_post_layout", _make_fake_post(posted, (200, {"ok": True, "ref": "terminal:abcd1234"}))
    )

    rc = layout.main(["open", "terminal"])
    assert rc == 0
    assert posted == [("open", {"ref": "service:terminal", "new_group": False})]
    assert capsys.readouterr().out.strip() == "terminal:abcd1234"


def test_open_without_returned_ref_emits_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Non-terminal ``open`` responses (no ``ref`` field) must leave stdout
    empty: callers parsing the script's stdout rely on it being silent
    unless the server explicitly returns a synchronously-allocated ref."""
    apps_file = tmp_path / "applications.toml"
    _write_apps_toml(apps_file, ["web"])
    monkeypatch.setenv(layout.ENV_APPLICATIONS_FILE, str(apps_file))

    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))

    rc = layout.main(["open", "web"])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_split_terminal_prints_returned_ref_to_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``split terminal`` shares the synchronous ref-return contract with
    ``open terminal`` since both go through the same allocation path."""
    monkeypatch.setattr(layout, "_wait_for_registration", lambda *a, **kw: True)
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        layout, "_post_layout", _make_fake_post(posted, (200, {"ok": True, "ref": "terminal:beef0000"}))
    )

    rc = layout.main(["split", "terminal", "--relative-to", "self", "--direction", "below"])
    assert rc == 0
    op, args = posted[0]
    assert op == "split"
    assert args["ref"] == "service:terminal"
    assert capsys.readouterr().out.strip() == "terminal:beef0000"


def test_open_url_prefix_alias_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``url:https://...`` alias normalizes to the bare URL ref."""
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))

    rc = layout.main(["open", "url:https://example.com"])
    assert rc == 0
    assert posted == [("open", {"ref": "https://example.com", "new_group": False})]


def test_split_accepts_external_url_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """``split`` accepts an external ``https://`` URL as the new panel."""
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))

    rc = layout.main(["split", "https://example.com", "--relative-to", "self"])
    assert rc == 0
    op, args = posted[0]
    assert op == "split"
    assert args["ref"] == "https://example.com"
    assert args["relative_to"] == "self"


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
    assert exc_info.value.code == layout.EXIT_ERROR
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


def test_network_failure_returns_exit_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Server-unreachable folds into the generic ``EXIT_ERROR`` -- the
    specific cause is in stderr, where wrapper scripts that care can
    surface it without needing a distinct exit code."""
    monkeypatch.setattr(layout, "_post_layout", lambda op, args: (-1, "Connection refused"))
    rc = layout.main(["refresh", "web"])
    assert rc == layout.EXIT_ERROR


def test_conflict_returns_distinct_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mutex contention is the one error class that keeps its own exit
    code: callers may want to retry-with-backoff on conflict but not on
    any other failure, so branching has to be possible from the exit
    code alone."""
    body = {
        "detail": "Another layout op is in flight",
        "retry_after_ms": 500,
        "in_flight": {"agent_id": "other-agent", "operation": "move", "args": {}, "started_at": 1700000000.0},
    }
    monkeypatch.setattr(layout, "_post_layout", lambda op, args: (409, body))
    rc = layout.main(["focus", "service:web"])
    assert rc == layout.EXIT_CONFLICT
    assert rc != layout.EXIT_ERROR
    err = capsys.readouterr().err
    assert "agent_id=other-agent" in err
    assert "op=move" in err


def test_not_found_folds_into_exit_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(layout, "_post_layout", lambda op, args: (404, {"detail": "unknown ref"}))
    rc = layout.main(["focus", "service:nonexistent"])
    assert rc == layout.EXIT_ERROR


def test_bad_request_folds_into_exit_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(layout, "_post_layout", lambda op, args: (400, {"detail": "bad arg"}))
    rc = layout.main(["close", "service:web"])
    assert rc == layout.EXIT_ERROR


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


# ---------- New surface: within direction, where, wait-stable, no-op, compact ----------


def test_split_within_direction_is_accepted_and_passed_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--direction=within`` is the single-call form of "tab into the
    anchor's own group" -- it must reach the server verbatim so the
    frontend's ``isWithinDirection`` branch can route through the
    ``referenceGroup`` placement path."""
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))
    monkeypatch.setattr(layout, "_wait_for_registration", lambda *a, **kw: True)

    rc = layout.main(["split", "service:web", "--relative-to", "chat:alice", "--direction", "within"])
    assert rc == 0
    op, args = posted[0]
    assert op == "split"
    assert args["direction"] == "within"
    assert args["relative_to"] == "chat:alice"
    assert args["ref"] == "service:web"


def test_move_within_direction_is_accepted_and_passed_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """The new ``within`` direction works on ``move`` too -- relocating a
    panel into another panel's group as a tab."""
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))

    rc = layout.main(
        ["move", "service:web", "--relative-to", "chat:alice", "--direction", "within"]
    )
    assert rc == 0
    op, args = posted[0]
    assert op == "move"
    assert args["direction"] == "within"


def test_split_within_with_new_group_is_rejected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--new-group`` is meaningless with ``--direction=within`` (within
    tabs into the anchor's own group; a fresh group would defeat the
    point). The CLI must reject the combination before posting."""
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))
    monkeypatch.setattr(layout, "_wait_for_registration", lambda *a, **kw: True)

    rc = layout.main(
        ["split", "service:web", "--relative-to", "chat:alice", "--direction", "within", "--new-group"]
    )
    assert rc == layout.EXIT_ERROR
    assert posted == []
    err = capsys.readouterr().err
    assert "--new-group" in err and "within" in err


def test_move_within_with_new_group_is_rejected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))

    rc = layout.main(
        ["move", "service:web", "--relative-to", "chat:alice", "--direction", "within", "--new-group"]
    )
    assert rc == layout.EXIT_ERROR
    assert posted == []
    err = capsys.readouterr().err
    assert "--new-group" in err and "within" in err


def test_inspect_compact_default_renders_one_line_per_group(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Default ``inspect`` is the compact text view -- not YAML. Each leaf
    is a single bracketed tab list; ``panel_id`` is hidden (verbose-only).
    The branch header shows ``arrangement`` (``row`` / ``column``)."""
    layout_obj = {
        "active_panel": "1",
        "panels": [],
        "tree": {
            "type": "branch",
            "arrangement": "row",
            "size_ratio": 1.0,
            "children": [
                {
                    "type": "leaf",
                    "size_ratio": 0.4,
                    "panels": [{"ref": "chat:alice", "panel_id": "chat-1", "active": True}],
                },
                {
                    "type": "leaf",
                    "size_ratio": 0.6,
                    "panels": [{"ref": "service:web", "panel_id": "p-web", "active": True}],
                },
            ],
        },
    }
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post([], (200, {"ok": True, "layout": layout_obj})))

    rc = layout.main(["inspect"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "row size=1.0" in out
    assert "[chat:alice*]" in out
    assert "[service:web*]" in out
    # ``panel_id`` is verbose-only; the compact view must not leak it.
    assert "panel_id" not in out
    assert "chat-1" not in out


def test_inspect_verbose_emits_yaml_with_panel_ids(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--verbose`` restores the previous YAML-tree-dump rendering,
    including ``panel_id`` and ``arrangement`` (the renamed field)."""
    layout_obj = {
        "active_panel": "1",
        "panels": [{"ref": "chat:alice", "panel_id": "chat-1"}],
        "tree": {
            "type": "branch",
            "arrangement": "row",
            "size_ratio": 1.0,
            "children": [
                {"type": "leaf", "size_ratio": 1.0,
                 "panels": [{"ref": "chat:alice", "panel_id": "chat-1", "active": True}]},
            ],
        },
    }
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post([], (200, {"ok": True, "layout": layout_obj})))

    rc = layout.main(["inspect", "--verbose"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "arrangement: row" in out
    assert "panel_id: chat-1" in out


def test_where_shows_tab_mates_and_cardinal_neighbors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``where <ref>`` is the focused introspection verb: it locates one
    panel's group, lists its tab-mates, and reports the cardinal-neighbor
    groups derived structurally from the inspect tree."""
    layout_obj = {
        "active_panel": "g-chat",
        "panels": [
            {"ref": "chat:alice"},
            {"ref": "terminal:abc"},
            {"ref": "service:web"},
        ],
        "tree": {
            "type": "branch",
            "arrangement": "row",
            "size_ratio": 1.0,
            "children": [
                {
                    "type": "leaf",
                    "size_ratio": 0.4,
                    "panels": [
                        {"ref": "chat:alice", "active": True, "title": "alice"},
                        {"ref": "terminal:abc"},
                    ],
                },
                {
                    "type": "leaf",
                    "size_ratio": 0.6,
                    "panels": [{"ref": "service:web", "active": True}],
                },
            ],
        },
    }
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post([], (200, {"ok": True, "layout": layout_obj})))

    rc = layout.main(["where", "chat:alice"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ref:" in out and "chat:alice" in out
    # Tab-mates (active tab marked with ``*``)
    assert "chat:alice*" in out and "terminal:abc" in out
    # Right neighbor is the service:web group; no left neighbor.
    assert "service:web*" in out
    # Compact format pads direction labels to 7 chars.
    assert "left    -" in out


def test_where_missing_ref_returns_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``where`` on an unknown ref must fail loudly rather than silently
    rendering an empty group view."""
    layout_obj = {"active_panel": None, "panels": [], "tree": None}
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post([], (200, {"ok": True, "layout": layout_obj})))

    rc = layout.main(["where", "chat:nobody"])
    assert rc == layout.EXIT_ERROR
    err = capsys.readouterr().err
    assert "not currently open" in err


def test_rename_emits_diff_after_observed_change(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A successful mutating op prints a one-line diff to stderr after the
    new state is observable via inspect. Reuses ``_run_mutating_op``'s
    wait-stable path; the env-var bypass is removed for this test."""
    # Drop the autouse bypass so the wait-stable code path runs.
    monkeypatch.delenv(layout.ENV_NO_WAIT_STABLE, raising=False)

    layouts = iter([
        {"active_panel": None, "panels": [{"ref": "chat:alice", "title": "alice"}], "tree": None},
        {"active_panel": None, "panels": [{"ref": "chat:alice", "title": "Alice (lead)"}], "tree": None},
    ])

    def fake_post(op: str, args: dict[str, Any]) -> tuple[int, dict[str, Any] | str]:
        if op == "inspect":
            return 200, {"ok": True, "layout": next(layouts)}
        return 200, {"ok": True}

    monkeypatch.setattr(layout, "_post_layout", fake_post)

    rc = layout.main(["rename", "chat:alice", "Alice (lead)"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "renamed chat:alice" in err
    assert "'alice'" in err and "'Alice (lead)'" in err


def test_rename_emits_noop_message_when_title_already_matches(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When the pre-op state already satisfies the predicate, the op is a
    no-op: stderr signals it explicitly and the op is NOT posted."""
    monkeypatch.delenv(layout.ENV_NO_WAIT_STABLE, raising=False)

    posted: list[tuple[str, dict[str, Any]]] = []

    def fake_post(op: str, args: dict[str, Any]) -> tuple[int, dict[str, Any] | str]:
        if op == "inspect":
            return 200, {
                "ok": True,
                "layout": {"active_panel": None, "panels": [{"ref": "chat:alice", "title": "frozen"}], "tree": None},
            }
        posted.append((op, args))
        return 200, {"ok": True}

    monkeypatch.setattr(layout, "_post_layout", fake_post)

    rc = layout.main(["rename", "chat:alice", "frozen"])
    assert rc == 0
    # No-op: the mutation op was never POSTed (only the inspect snapshot).
    assert posted == []
    err = capsys.readouterr().err
    assert "no change: chat:alice is already titled 'frozen'" in err


def test_maximize_is_unobservable_and_notes_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``maximize`` / ``restore`` / ``refresh`` do not affect
    inspect-observable state -- the wait-stable path is skipped and the
    stderr message makes that explicit."""
    monkeypatch.delenv(layout.ENV_NO_WAIT_STABLE, raising=False)

    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))

    rc = layout.main(["maximize", "service:web"])
    assert rc == 0
    # Only the broadcast went out -- no inspect probes.
    assert posted == [("maximize", {"ref": "service:web"})]
    err = capsys.readouterr().err
    assert "no observable layout-state change" in err
