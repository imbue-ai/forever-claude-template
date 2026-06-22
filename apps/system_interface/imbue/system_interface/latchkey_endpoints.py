"""HTTP endpoint for `/api/latchkey/*`.

Resolves a latchkey permission scope (e.g. ``slack-api``) to its catalog entry
-- the human-readable service name plus per-permission descriptions -- by
querying the latchkey gateway's per-service catalog endpoint
(``GET /permissions/available/<service>``, the same one agents use). The
``system_interface`` backend runs inside the agent container, so it inherits the
agent's gateway address and credentials from the environment and is authorized
for that per-service endpoint.

The frontend calls this to label a permission-request card with the real service
name and to show permission descriptions, rather than bundling a copy of the
gateway catalog that would drift over time.

The gateway calls are synchronous: resolving one scope is one or two sequential
catalog lookups with no concurrency to exploit, and the rest of this inner chat
interface is plain sync request handlers, so a sync ``httpx.Client`` keeps it
simple. A gateway that can't be reached or answers with an error status is
surfaced to the frontend as a 502 (distinct from a genuine 404 "no such scope"),
rather than being swallowed.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
from flask import Flask
from flask import Response
from loguru import logger as _loguru_logger

from imbue.system_interface.app_context import get_state
from imbue.system_interface.models import LatchkeyPermissionInfo
from imbue.system_interface.models import LatchkeyScopeInfo

logger = _loguru_logger

# Injected into every agent container by the latchkey agent setup in mngr. These
# names (and the header names below) are a stable agent<->gateway contract; we
# read them rather than bundle any gateway state of our own.
_ENV_GATEWAY = "LATCHKEY_GATEWAY"
_ENV_GATEWAY_PASSWORD = "LATCHKEY_GATEWAY_PASSWORD"
_ENV_GATEWAY_PERMISSIONS_OVERRIDE = "LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE"
_HEADER_PASSWORD = "X-Latchkey-Gateway-Password"
_HEADER_PERMISSIONS_OVERRIDE = "X-Latchkey-Gateway-Permissions-Override"

# Per-service catalog responses are cached (on the app's ``SystemInterfaceState``)
# keyed by service name; ``None`` records a 404 so a non-service scope-prefix
# isn't re-requested.
ServiceCatalog = tuple[dict[str, Any], ...]
CatalogCache = dict[str, ServiceCatalog | None]


def _json_response(content: object, status_code: int = 200) -> Response:
    body = json.dumps(content, separators=(",", ":"), ensure_ascii=False)
    return Response(body, status=status_code, mimetype="application/json")


def candidate_services(scope: str) -> list[str]:
    """The service-name candidates for a scope, longest hyphen-prefix first.

    The gateway catalog is keyed by raw service name (``slack``,
    ``google-gmail``, ``github``), and a scope is always that key plus a
    transport suffix (``slack-api``, ``google-gmail-api``, ``github-rest-api``).
    So the service is one of the scope's hyphen-prefixes; trying the longest
    first finds it in one or two requests and disambiguates the awkward case
    ``github-rest-api`` -> ``github`` (not ``github-rest``).
    """
    parts = scope.split("-")
    return ["-".join(parts[:count]) for count in range(len(parts), 0, -1)]


def _get_service_catalog(
    client: httpx.Client,
    base_url: str,
    headers: dict[str, str],
    service: str,
    cache: CatalogCache,
) -> ServiceCatalog | None:
    """Fetch (and cache) the gateway's catalog entries for one service.

    Returns the entries, or ``None`` when the gateway has no such service (a
    404 -- expected while walking the scope's service-name candidates). Raises
    ``httpx.HTTPError`` when the gateway can't be reached or answers with a
    non-404 error status, so the caller can surface a 502 rather than a
    misleading 404. Only a definitive 404 is cached; an error propagates
    uncached so a later request can retry.
    """
    if service in cache:
        return cache[service]
    url = f"{base_url.rstrip('/')}/permissions/available/{service}"
    response = client.get(url, headers=headers)
    if response.status_code == 404:
        cache[service] = None
        return None
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        return None
    entries = tuple(entry for entry in data if isinstance(entry, dict))
    cache[service] = entries
    return entries


def _to_scope_info(scope: str, entry: dict[str, Any]) -> LatchkeyScopeInfo | None:
    """Build the response model from a catalog entry, pulling only the fields we
    expose (so extra gateway fields are tolerated). Returns ``None`` if the entry
    lacks a usable display name."""
    display_name = entry.get("display_name")
    if not isinstance(display_name, str) or not display_name:
        return None
    description = entry.get("description")
    raw_permissions = entry.get("permissions")
    permissions = tuple(
        LatchkeyPermissionInfo(
            name=permission["name"],
            description=permission.get("description") if isinstance(permission.get("description"), str) else None,
        )
        for permission in (raw_permissions if isinstance(raw_permissions, list) else [])
        if isinstance(permission, dict) and isinstance(permission.get("name"), str)
    )
    return LatchkeyScopeInfo(
        scope=scope,
        display_name=display_name,
        description=description if isinstance(description, str) else None,
        permissions=permissions,
    )


def resolve_scope_info(
    client: httpx.Client,
    base_url: str,
    password: str,
    permissions_override: str,
    scope: str,
    cache: CatalogCache,
) -> LatchkeyScopeInfo | None:
    """Resolve a scope to its catalog entry via the gateway, or ``None``.

    Walks the scope's service-name candidates (longest prefix first) and returns
    the entry from the first service whose catalog contains the scope. Propagates
    ``httpx.HTTPError`` from the gateway calls.
    """
    headers = {_HEADER_PASSWORD: password, _HEADER_PERMISSIONS_OVERRIDE: permissions_override}
    for service in candidate_services(scope):
        entries = _get_service_catalog(client, base_url, headers, service, cache)
        if entries is None:
            continue
        for entry in entries:
            if entry.get("scope") == scope:
                return _to_scope_info(scope, entry)
    return None


def get_scope_info(scope: str) -> Response:
    """GET /api/latchkey/scopes/{scope} -- catalog info for a permission scope.

    503 when the gateway env isn't configured (e.g. running outside an agent
    container); 502 when the gateway can't be reached or errors; 404 when the
    scope isn't in the gateway catalog.
    """
    base_url = os.environ.get(_ENV_GATEWAY)
    password = os.environ.get(_ENV_GATEWAY_PASSWORD)
    permissions_override = os.environ.get(_ENV_GATEWAY_PERMISSIONS_OVERRIDE)
    if not base_url or not password or not permissions_override:
        return _json_response({"detail": "latchkey gateway is not configured"}, status_code=503)
    state = get_state()
    client: httpx.Client = state.latchkey_http_client
    cache: CatalogCache = state.latchkey_catalog_cache
    try:
        # Serialize concurrent resolves so two request threads don't both fetch
        # the same uncached service catalog.
        with state.latchkey_lock:
            info = resolve_scope_info(client, base_url, password, permissions_override, scope, cache)
    except httpx.HTTPError as error:
        logger.warning("latchkey gateway request for scope {!r} failed: {}", scope, error)
        return _json_response({"detail": "latchkey gateway request failed"}, status_code=502)
    if info is None:
        return _json_response({"detail": f"no catalog entry for scope {scope!r}"}, status_code=404)
    return _json_response(info.model_dump())


def register_routes(application: Flask) -> None:
    """Wire `/api/latchkey/*` endpoints onto the Flask application."""
    application.add_url_rule("/api/latchkey/scopes/<scope>", view_func=get_scope_info, methods=["GET"])
