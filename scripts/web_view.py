#!/usr/bin/env python3
"""Agent-facing helper for surfacing and refreshing web-service tabs.

Subcommands:
    list                 Print registered service names, one per line.
    open <name>          Ask the workspace UI to open the named service.
    refresh <name>       Ask the workspace UI to reload the named service.

``open`` and ``refresh`` POST to a loopback-only endpoint on the workspace
server. ``open`` first verifies the service is registered in
``runtime/applications.toml``, with a short retry window for the watchdog
that picks up newly-registered services.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import tomlkit

APPLICATIONS_FILE = Path("runtime/applications.toml")
DEFAULT_WORKSPACE_URL = "http://127.0.0.1:8000"
ENV_WORKSPACE_URL = "MINDS_WORKSPACE_SERVER_URL"

# How long ``open`` waits for a freshly-registered service to appear before
# giving up. The bootstrap-managed forward_port.py call races with the agent
# invoking this script right after build-web-service, so we tolerate a brief
# window where the entry is not yet visible.
_OPEN_REGISTRATION_TIMEOUT_SECONDS = 5.0
_OPEN_REGISTRATION_POLL_INTERVAL_SECONDS = 0.25

# Reserved entries that aren't user-facing tabs.
_HIDDEN_SERVICES = frozenset({"system_interface"})


def _workspace_base_url() -> str:
    return os.environ.get(ENV_WORKSPACE_URL, DEFAULT_WORKSPACE_URL).rstrip("/")


def _read_application_names(path: Path) -> list[str]:
    if not path.exists():
        return []
    with open(path, "rb") as f:
        doc = tomlkit.load(f)
    apps = doc.get("applications", [])
    names: list[str] = []
    for app in apps:
        name = app.get("name") if hasattr(app, "get") else None
        if isinstance(name, str) and name:
            names.append(name)
    return names


def _is_service_registered(name: str) -> bool:
    return name in _read_application_names(APPLICATIONS_FILE)


def _wait_for_registration(name: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if _is_service_registered(name):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_OPEN_REGISTRATION_POLL_INTERVAL_SECONDS)


def _post(url: str) -> tuple[int, str]:
    """POST to ``url`` with an empty body. Returns (status, body_text).

    Connection-level failures are surfaced as (-1, error_str).
    """
    req = urllib.request.Request(url, data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10.0) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        return -1, str(e.reason)


def _cmd_list(_args: argparse.Namespace) -> int:
    for name in _read_application_names(APPLICATIONS_FILE):
        if name in _HIDDEN_SERVICES:
            continue
        print(name)
    return 0


def _cmd_open(args: argparse.Namespace) -> int:
    name = args.name
    if not _wait_for_registration(name, _OPEN_REGISTRATION_TIMEOUT_SECONDS):
        print(
            f"error: service {name!r} is not registered in {APPLICATIONS_FILE} "
            f"after waiting {_OPEN_REGISTRATION_TIMEOUT_SECONDS:.0f}s. "
            f"Did you forward_port.py / start the service?",
            file=sys.stderr,
        )
        return 2

    url = f"{_workspace_base_url()}/api/open-tab/{name}/broadcast"
    status, body = _post(url)
    return _report_post_result("open", name, url, status, body)


def _cmd_refresh(args: argparse.Namespace) -> int:
    name = args.name
    url = f"{_workspace_base_url()}/api/refresh-service/{name}/broadcast"
    status, body = _post(url)
    return _report_post_result("refresh", name, url, status, body)


def _report_post_result(action: str, name: str, url: str, status: int, body: str) -> int:
    if status == 200:
        return 0
    if status == -1:
        print(f"error: could not reach workspace server at {url}: {body}", file=sys.stderr)
        return 3
    detail = body
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict) and "detail" in parsed:
            detail = str(parsed["detail"])
    except json.JSONDecodeError:
        pass
    print(f"error: {action} {name!r} failed (HTTP {status}): {detail}", file=sys.stderr)
    return 4


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_list = subparsers.add_parser("list", help="List registered services")
    p_list.set_defaults(func=_cmd_list)

    p_open = subparsers.add_parser("open", help="Open the named service as a tab in the UI")
    p_open.add_argument("name", help="Service name (must match runtime/applications.toml)")
    p_open.set_defaults(func=_cmd_open)

    p_refresh = subparsers.add_parser("refresh", help="Reload any open tab for the named service")
    p_refresh.add_argument("name", help="Service name (must match runtime/applications.toml)")
    p_refresh.set_defaults(func=_cmd_refresh)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
