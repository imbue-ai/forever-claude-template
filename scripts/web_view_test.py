"""Tests for the agent-facing web_view.py helper.

These tests exercise the behavior an agent depends on: that ``list`` filters
out the chrome service, that ``open`` waits for registration before POSTing,
and that ``open`` / ``refresh`` hit the correct loopback URLs.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
import tomlkit

_SCRIPT = Path(__file__).parent / "web_view.py"
_spec = importlib.util.spec_from_file_location("web_view", _SCRIPT)
assert _spec is not None and _spec.loader is not None
web_view = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(web_view)


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


def test_list_omits_system_interface(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    apps_file = tmp_path / "applications.toml"
    _write_apps_toml(apps_file, ["web", "system_interface", "api"])
    monkeypatch.setattr(web_view, "APPLICATIONS_FILE", apps_file)

    rc = web_view.main(["list"])
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["web", "api"]


def test_open_posts_to_open_tab_endpoint_after_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    apps_file = tmp_path / "applications.toml"
    _write_apps_toml(apps_file, ["web"])
    monkeypatch.setattr(web_view, "APPLICATIONS_FILE", apps_file)
    monkeypatch.setenv(web_view.ENV_WORKSPACE_URL, "http://127.0.0.1:8000")

    posted: list[str] = []

    def fake_post(url: str) -> tuple[int, str]:
        posted.append(url)
        return 200, "{}"

    monkeypatch.setattr(web_view, "_post", fake_post)

    rc = web_view.main(["open", "web"])
    assert rc == 0
    assert posted == ["http://127.0.0.1:8000/api/open-tab/web/broadcast"]


def test_open_fails_when_service_not_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    apps_file = tmp_path / "applications.toml"
    _write_apps_toml(apps_file, ["other"])
    monkeypatch.setattr(web_view, "APPLICATIONS_FILE", apps_file)
    monkeypatch.setattr(web_view, "_OPEN_REGISTRATION_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(web_view, "_OPEN_REGISTRATION_POLL_INTERVAL_SECONDS", 0.01)

    posted: list[str] = []

    def fake_post(url: str) -> tuple[int, str]:
        posted.append(url)
        return 200, "{}"

    monkeypatch.setattr(web_view, "_post", fake_post)

    rc = web_view.main(["open", "web"])
    assert rc == 2
    assert posted == []
    err = capsys.readouterr().err
    assert "not registered" in err


def test_refresh_posts_to_refresh_service_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(web_view.ENV_WORKSPACE_URL, "http://127.0.0.1:8000")
    posted: list[str] = []

    def fake_post(url: str) -> tuple[int, str]:
        posted.append(url)
        return 200, "{}"

    monkeypatch.setattr(web_view, "_post", fake_post)

    rc = web_view.main(["refresh", "web"])
    assert rc == 0
    assert posted == ["http://127.0.0.1:8000/api/refresh-service/web/broadcast"]


def test_post_failure_returns_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Network-level failure surfaces as exit code 3."""

    def failing_post(url: str) -> tuple[int, str]:
        return -1, "Connection refused"

    monkeypatch.setattr(web_view, "_post", failing_post)
    rc = web_view.main(["refresh", "web"])
    assert rc == 3


def test_http_error_returns_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 4xx/5xx from the server surfaces as exit code 4."""

    def err_post(url: str) -> tuple[int, str]:
        return 500, '{"detail": "boom"}'

    monkeypatch.setattr(web_view, "_post", err_post)
    rc: Any = web_view.main(["refresh", "web"])
    assert rc == 4
