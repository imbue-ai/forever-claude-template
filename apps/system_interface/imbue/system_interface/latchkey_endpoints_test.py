"""Tests for the latchkey scope-resolution endpoint.

The gateway is faked with an ``httpx.MockTransport`` injected into the app via
``create_application(latchkey_http_client=...)``, so the resolver (service-prefix
walking, scope matching, caching, malformed-entry handling) is exercised
end-to-end through the Flask test client without a real latchkey gateway.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from imbue.system_interface.latchkey_endpoints import candidate_services
from imbue.system_interface.server import create_application

_GATEWAY_ENV = ("LATCHKEY_GATEWAY", "LATCHKEY_GATEWAY_PASSWORD", "LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE")

SLACK_CATALOG = [
    {
        "scope": "slack-api",
        "display_name": "Slack",
        "description": "Any interaction with the Slack API.",
        "permissions": [
            {"name": "slack-read-all", "description": "All read operations across the Slack API."},
            {"name": "slack-write-all", "description": "All write operations across the Slack API."},
        ],
    }
]

# A multi-scope service whose REST scope does NOT match a naive "-api" strip.
GITHUB_CATALOG = [
    {
        "scope": "github-rest-api",
        "display_name": "GitHub (REST API)",
        "description": "GitHub REST.",
        "permissions": [],
    },
    {"scope": "github-git", "display_name": "GitHub (git)", "description": "GitHub git.", "permissions": []},
]


def _mock_gateway_client(catalogs: dict[str, list[dict[str, Any]]], calls: list[str]) -> httpx.Client:
    """An httpx client whose transport serves the gateway's per-service catalog
    endpoint from ``catalogs`` (404 for unknown services), recording each
    requested service name in ``calls``."""

    def handler(request: httpx.Request) -> httpx.Response:
        service = request.url.path.rsplit("/", 1)[-1]
        calls.append(service)
        if service in catalogs:
            return httpx.Response(200, json=catalogs[service])
        return httpx.Response(404, json={"detail": "unknown service"})

    return httpx.Client(transport=httpx.MockTransport(handler))


def _configure_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LATCHKEY_GATEWAY", "http://gateway.invalid")
    monkeypatch.setenv("LATCHKEY_GATEWAY_PASSWORD", "secret")
    monkeypatch.setenv("LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE", "jwt")


def test_candidate_services_yields_longest_prefix_first() -> None:
    assert candidate_services("slack-api") == ["slack-api", "slack"]
    assert candidate_services("google-gmail-api") == ["google-gmail-api", "google-gmail", "google"]
    assert candidate_services("aws") == ["aws"]


def test_resolves_simple_scope_via_service_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_gateway(monkeypatch)
    calls: list[str] = []
    application = create_application(latchkey_http_client=_mock_gateway_client({"slack": SLACK_CATALOG}, calls))
    client = application.test_client()
    response = client.get("/api/latchkey/scopes/slack-api")
    assert response.status_code == 200
    body = response.get_json()
    assert body["display_name"] == "Slack"
    assert body["scope"] == "slack-api"
    assert [permission["name"] for permission in body["permissions"]] == ["slack-read-all", "slack-write-all"]
    assert body["permissions"][0]["description"] == "All read operations across the Slack API."
    # Tried the full scope (404) then the service key (200).
    assert calls == ["slack-api", "slack"]


def test_resolves_multi_segment_service_disambiguating_github(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_gateway(monkeypatch)
    calls: list[str] = []
    application = create_application(latchkey_http_client=_mock_gateway_client({"github": GITHUB_CATALOG}, calls))
    client = application.test_client()
    response = client.get("/api/latchkey/scopes/github-rest-api")
    assert response.status_code == 200
    assert response.get_json()["display_name"] == "GitHub (REST API)"
    # Walked down to the "github" service key, past the non-service prefixes.
    assert calls == ["github-rest-api", "github-rest", "github"]


def test_caches_catalog_across_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_gateway(monkeypatch)
    calls: list[str] = []
    application = create_application(latchkey_http_client=_mock_gateway_client({"github": GITHUB_CATALOG}, calls))
    client = application.test_client()
    assert client.get("/api/latchkey/scopes/github-rest-api").status_code == 200
    assert client.get("/api/latchkey/scopes/github-git").status_code == 200
    # The "github" catalog is fetched once and reused for the second scope.
    assert calls.count("github") == 1
    assert "github-git" in calls


def test_unknown_scope_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_gateway(monkeypatch)
    application = create_application(latchkey_http_client=_mock_gateway_client({"slack": SLACK_CATALOG}, []))
    client = application.test_client()
    assert client.get("/api/latchkey/scopes/madeup-api").status_code == 404


def test_malformed_entry_without_display_name_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_gateway(monkeypatch)
    application = create_application(latchkey_http_client=_mock_gateway_client({"x": [{"scope": "x-api"}]}, []))
    client = application.test_client()
    assert client.get("/api/latchkey/scopes/x-api").status_code == 404


def test_unreachable_gateway_returns_502(monkeypatch: pytest.MonkeyPatch) -> None:
    # A gateway we can't reach is distinct from a scope that genuinely has no
    # catalog entry: surface it as a 502, not a misleading 404.
    _configure_gateway(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("gateway down", request=request)

    application = create_application(latchkey_http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    client = application.test_client()
    assert client.get("/api/latchkey/scopes/slack-api").status_code == 502


def test_gateway_error_status_returns_502(monkeypatch: pytest.MonkeyPatch) -> None:
    # A non-404 error status from the gateway (e.g. a 500) is an upstream
    # failure, surfaced as a 502 rather than swallowed into a 404.
    _configure_gateway(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    application = create_application(latchkey_http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    client = application.test_client()
    assert client.get("/api/latchkey/scopes/slack-api").status_code == 502


def test_returns_503_when_gateway_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _GATEWAY_ENV:
        monkeypatch.delenv(name, raising=False)
    client = create_application().test_client()
    assert client.get("/api/latchkey/scopes/slack-api").status_code == 503
