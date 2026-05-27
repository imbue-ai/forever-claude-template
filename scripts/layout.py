#!/usr/bin/env python3
"""Agent-facing helper for inspecting and mutating the workspace dockview layout.

Subcommands:
    list                                List addressable services + agents (open/running flags).
    inspect                             Describe the live dockview state as a ref-resolved tree.
    open <ref-or-service>               Surface a service (focus-if-open, else tab into / split next to caller's chat).
    focus <ref-or-service>              Activate the named panel within its group.
    split <ref-or-service> [...]        Add a panel relative to another panel; tabs into an existing adjacent group by default.
    close <ref-or-service>              Remove the named panel.
    move <ref-or-service> --relative-to <ref-or-service> [...]  Relocate a panel; iframe DOM is preserved.
    rename <ref-or-service> <title>     Update the panel's tab title.
    maximize <ref-or-service>           Maximize the panel's group within the dockview.
    restore                             Exit a maximized group.
    replace-url <ref-or-service> <url>  Swap an iframe's src (service:<name>[/<path>] or https://...).
    refresh <ref-or-service>            Reload one iframe; ``service:<name>`` reloads all iframes for that service.

Every ref-accepting argument (positional ref, ``--relative-to``) accepts a bare
service name as shorthand for ``service:<name>``. ``open`` and ``split`` also
accept an external ``https://`` URL (bare, or with an optional ``url:`` prefix)
as the panel to create -- it surfaces as an ad-hoc URL tab.

``open`` / ``split`` / ``move`` default to *tabbing into an existing group*
that already lives in the requested direction relative to the anchor; pass
``--new-group`` to force a fresh column / row instead.

All ops POST a single body ``{op, args, agent_id}`` to a loopback-only endpoint
on the workspace server. The caller's ``MNGR_AGENT_ID`` is sent both in the
JSON body and as the ``X-Mngr-Agent-Id`` request header for telemetry.

Output for ``list`` and ``inspect`` is YAML by default; pass ``--json`` for
raw object output.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import tomlkit
import yaml

DEFAULT_APPLICATIONS_FILE = "runtime/applications.toml"
ENV_APPLICATIONS_FILE = "MINDS_APPLICATIONS_FILE"
DEFAULT_WORKSPACE_URL = "http://127.0.0.1:8000"
ENV_WORKSPACE_URL = "MINDS_WORKSPACE_SERVER_URL"
ENV_MNGR_AGENT_ID = "MNGR_AGENT_ID"
MNGR_AGENT_ID_HEADER = "X-Mngr-Agent-Id"

# How long ``open`` / ``split`` wait for a freshly-registered service to
# appear before giving up. The bootstrap-managed forward_port.py call races
# with the agent invoking this script right after build-web-service, so we
# tolerate a brief window where the entry is not yet visible.
_REGISTRATION_TIMEOUT_SECONDS = 5.0
_REGISTRATION_POLL_INTERVAL_SECONDS = 0.25

# Set of accepted ref prefixes.
_REF_PREFIXES = ("service:", "chat:", "terminal:", "url:", "subagent:")
# Set of accepted directions for split/move.
_DIRECTIONS = ("left", "right", "above", "below")


# Exit codes -- distinct so wrapper scripts can branch on them. Argparse uses
# exit code 2 for any CLI usage error (unknown subcommand, invalid choice,
# missing required argument), so the layout-specific codes start at 10 to
# avoid that collision.
EXIT_OK = 0
EXIT_NOT_REGISTERED = 10
EXIT_NETWORK = 11
EXIT_HTTP_ERROR = 12
EXIT_CONFLICT = 13
EXIT_NOT_FOUND = 14
EXIT_BAD_REQUEST = 15


def _workspace_base_url() -> str:
    return os.environ.get(ENV_WORKSPACE_URL, DEFAULT_WORKSPACE_URL).rstrip("/")


def _mngr_agent_id() -> str:
    return os.environ.get(ENV_MNGR_AGENT_ID, "")


def _applications_file() -> Path:
    """Path to the agent's applications.toml registry.

    Defaults to ``runtime/applications.toml`` relative to cwd (the script
    is invoked from the agent's repo root). Override via
    ``MINDS_APPLICATIONS_FILE`` -- used by tests to point at a sandboxed
    fixture without depending on cwd.
    """
    return Path(os.environ.get(ENV_APPLICATIONS_FILE, DEFAULT_APPLICATIONS_FILE))


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
    return name in _read_application_names(_applications_file())


def _wait_for_registration(name: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if _is_service_registered(name):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_REGISTRATION_POLL_INTERVAL_SECONDS)


# Sentinel ref the frontend resolves to the caller's own chat panel. Valid as
# a ``--relative-to`` value for ``split`` / ``move`` and as a target for any
# op (e.g. ``focus self``).
_SELF_REF = "self"


def _normalize_ref(value: str) -> str:
    """Expand bare service-name shorthand into a full ``service:`` ref.

    Anything that already carries a known prefix is returned as-is, and the
    ``self`` sentinel (resolved frontend-side to the caller's chat panel)
    is preserved verbatim. An external ``https://`` URL -- bare or written
    with the optional ``url:`` alias -- normalizes to the bare URL, which
    the frontend treats as a creation ref for an ad-hoc URL panel. (The
    ``url:<hash>`` form, which addresses an already-open URL panel, is
    left untouched.)
    """
    if value == _SELF_REF:
        return value
    if value.startswith("https://"):
        return value
    if value.startswith("url:https://"):
        return value.removeprefix("url:")
    for prefix in _REF_PREFIXES:
        if value.startswith(prefix):
            return value
    return f"service:{value}"


def _validate_ref(ref: str) -> None:
    """Raise SystemExit if the ref carries an unknown prefix.

    The ``self`` sentinel and bare ``https://`` external URLs are accepted
    in addition to the prefix forms.
    """
    if ref == _SELF_REF:
        return
    if ref.startswith("https://"):
        return
    if not any(ref.startswith(p) for p in _REF_PREFIXES):
        sys.stderr.write(
            f"error: ref {ref!r} must start with one of {_REF_PREFIXES}, "
            f"be a bare service name, or be an https:// URL\n"
        )
        raise SystemExit(EXIT_BAD_REQUEST)


def _validate_replace_url(url: str) -> None:
    """Reject anything that isn't ``service:<name>[/<path>]`` or ``https://...``."""
    if url.startswith("service:") or url.startswith("https://"):
        return
    sys.stderr.write(
        f"error: replace-url accepts only ``service:<name>[/<path>]`` shorthand or a full https:// URL (got {url!r})\n"
    )
    raise SystemExit(EXIT_BAD_REQUEST)


def _post_layout(op: str, args: dict[str, Any]) -> tuple[int, dict[str, Any] | str]:
    """POST {op, args, agent_id} to /api/layout/broadcast and return (status, parsed_or_raw)."""
    url = f"{_workspace_base_url()}/api/layout/broadcast"
    body = json.dumps({"op": op, "args": args, "agent_id": _mngr_agent_id()}).encode("utf-8")
    headers = {"Content-Type": "application/json", MNGR_AGENT_ID_HEADER: _mngr_agent_id()}
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10.0) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return response.status, _maybe_parse_json(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        return e.code, _maybe_parse_json(raw)
    except urllib.error.URLError as e:
        return -1, str(e.reason)


def _maybe_parse_json(text: str) -> dict[str, Any] | str:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(parsed, dict):
        return parsed
    return text


def _report_failure(op: str, status: int, body: dict[str, Any] | str) -> int:
    """Translate (status, body) into a stderr message + exit code."""
    if status == -1:
        sys.stderr.write(f"error: could not reach workspace server: {body}\n")
        return EXIT_NETWORK
    detail: str = ""
    if isinstance(body, dict):
        detail = str(body.get("detail", body))
        if status == 409:
            in_flight = body.get("in_flight") or {}
            retry_ms = body.get("retry_after_ms")
            sys.stderr.write(
                f"error: layout op {op!r} rejected (HTTP 409 conflict): {detail}\n"
                f"  in-flight: agent_id={in_flight.get('agent_id')} op={in_flight.get('operation')} "
                f"args={in_flight.get('args')} started_at={in_flight.get('started_at')}\n"
                f"  retry_after_ms={retry_ms}\n"
            )
            return EXIT_CONFLICT
        if status == 404:
            sys.stderr.write(f"error: layout op {op!r} target not found (HTTP 404): {detail}\n")
            return EXIT_NOT_FOUND
        if status == 400:
            sys.stderr.write(f"error: layout op {op!r} rejected (HTTP 400): {detail}\n")
            return EXIT_BAD_REQUEST
    else:
        detail = body
    sys.stderr.write(f"error: layout op {op!r} failed (HTTP {status}): {detail}\n")
    return EXIT_HTTP_ERROR


def _emit_allocated_ref(body: dict[str, Any] | str) -> None:
    """Print the server-allocated ref to stdout when the response includes one.

    Today this only fires for ``open`` / ``split`` targeting
    ``service:terminal``: the server pre-mints the panel id (mirroring the
    UI's "New terminal" button, which creates a fresh tab on every click)
    and returns the resulting ``terminal:<hash>`` ref so callers can capture
    it for later ops without round-tripping through ``inspect``.
    """
    if isinstance(body, dict):
        ref = body.get("ref")
        if isinstance(ref, str) and ref:
            sys.stdout.write(ref)
            sys.stdout.write("\n")


def _emit_structured(data: Any, as_json: bool) -> None:
    if as_json:
        sys.stdout.write(json.dumps(data, indent=2))
        sys.stdout.write("\n")
    else:
        # ``sort_keys=False`` so the YAML preserves the server's intentional
        # ordering (e.g. tree-before-flat-panels, panels in tab order).
        yaml.safe_dump(data, sys.stdout, sort_keys=False, default_flow_style=False)


def _cmd_list(args: argparse.Namespace) -> int:
    status, body = _post_layout("list", {})
    if status != 200 or not isinstance(body, dict):
        return _report_failure("list", status, body)
    entries = body.get("entries", [])
    _emit_structured(entries, args.json)
    return EXIT_OK


def _cmd_inspect(args: argparse.Namespace) -> int:
    status, body = _post_layout("inspect", {})
    if status != 200 or not isinstance(body, dict):
        return _report_failure("inspect", status, body)
    _emit_structured(body.get("layout", {}), args.json)
    return EXIT_OK


def _cmd_open(args: argparse.Namespace) -> int:
    ref = _normalize_ref(args.target)
    if ref.startswith("service:"):
        service_name = ref.removeprefix("service:")
        if not _wait_for_registration(service_name, _REGISTRATION_TIMEOUT_SECONDS):
            sys.stderr.write(
                f"error: service {service_name!r} is not registered in {_applications_file()} "
                f"after waiting {_REGISTRATION_TIMEOUT_SECONDS:.0f}s. "
                f"Did you forward_port.py / start the service?\n"
            )
            return EXIT_NOT_REGISTERED
    _validate_ref(ref)
    payload: dict[str, Any] = {"ref": ref, "new_group": bool(args.new_group)}
    status, body = _post_layout("open", payload)
    if status != 200:
        return _report_failure("open", status, body)
    _emit_allocated_ref(body)
    return EXIT_OK


def _cmd_focus(args: argparse.Namespace) -> int:
    ref = _normalize_ref(args.ref)
    _validate_ref(ref)
    status, body = _post_layout("focus", {"ref": ref})
    if status != 200:
        return _report_failure("focus", status, body)
    return EXIT_OK


def _cmd_split(args: argparse.Namespace) -> int:
    ref = _normalize_ref(args.target)
    if ref.startswith("service:"):
        service_name = ref.removeprefix("service:")
        if not _wait_for_registration(service_name, _REGISTRATION_TIMEOUT_SECONDS):
            sys.stderr.write(
                f"error: service {service_name!r} is not registered in {_applications_file()} "
                f"after waiting {_REGISTRATION_TIMEOUT_SECONDS:.0f}s.\n"
            )
            return EXIT_NOT_REGISTERED
    _validate_ref(ref)
    relative_to = _normalize_ref(args.relative_to)
    _validate_ref(relative_to)
    payload: dict[str, Any] = {
        "ref": ref,
        "relative_to": relative_to,
        "direction": args.direction,
        "ratio": args.ratio,
        "new_group": bool(args.new_group),
    }
    status, body = _post_layout("split", payload)
    if status != 200:
        return _report_failure("split", status, body)
    _emit_allocated_ref(body)
    return EXIT_OK


def _cmd_close(args: argparse.Namespace) -> int:
    ref = _normalize_ref(args.ref)
    _validate_ref(ref)
    status, body = _post_layout("close", {"ref": ref})
    if status != 200:
        return _report_failure("close", status, body)
    return EXIT_OK


def _cmd_move(args: argparse.Namespace) -> int:
    ref = _normalize_ref(args.ref)
    _validate_ref(ref)
    relative_to = _normalize_ref(args.relative_to)
    _validate_ref(relative_to)
    payload: dict[str, Any] = {
        "ref": ref,
        "relative_to": relative_to,
        "direction": args.direction,
        "new_group": bool(args.new_group),
    }
    status, body = _post_layout("move", payload)
    if status != 200:
        return _report_failure("move", status, body)
    return EXIT_OK


def _cmd_rename(args: argparse.Namespace) -> int:
    ref = _normalize_ref(args.ref)
    _validate_ref(ref)
    status, body = _post_layout("rename", {"ref": ref, "title": args.title})
    if status != 200:
        return _report_failure("rename", status, body)
    return EXIT_OK


def _cmd_maximize(args: argparse.Namespace) -> int:
    ref = _normalize_ref(args.ref)
    _validate_ref(ref)
    status, body = _post_layout("maximize", {"ref": ref})
    if status != 200:
        return _report_failure("maximize", status, body)
    return EXIT_OK


def _cmd_restore(_args: argparse.Namespace) -> int:
    status, body = _post_layout("restore", {})
    if status != 200:
        return _report_failure("restore", status, body)
    return EXIT_OK


def _cmd_replace_url(args: argparse.Namespace) -> int:
    ref = _normalize_ref(args.ref)
    _validate_ref(ref)
    _validate_replace_url(args.url)
    status, body = _post_layout("replace-url", {"ref": ref, "url": args.url})
    if status != 200:
        return _report_failure("replace-url", status, body)
    return EXIT_OK


def _cmd_refresh(args: argparse.Namespace) -> int:
    ref = _normalize_ref(args.target)
    _validate_ref(ref)
    status, body = _post_layout("refresh", {"ref": ref})
    if status != 200:
        return _report_failure("refresh", status, body)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_list = subparsers.add_parser("list", help="List addressable services + agents")
    p_list.add_argument("--json", action="store_true", help="Emit JSON instead of YAML")
    p_list.set_defaults(func=_cmd_list)

    p_inspect = subparsers.add_parser("inspect", help="Describe the live dockview state")
    p_inspect.add_argument("--json", action="store_true", help="Emit JSON instead of YAML")
    p_inspect.set_defaults(func=_cmd_inspect)

    p_open = subparsers.add_parser("open", help="Surface a service in the UI")
    p_open.add_argument(
        "target",
        help="Service name, ref, or https:// URL (e.g. ``web``, ``service:web``, ``https://example.com``)",
    )
    p_open.add_argument(
        "--new-group",
        action="store_true",
        help=(
            "Force a brand-new dockview group instead of tabbing into an existing "
            "right-side group (the default reuses adjacent groups when present)."
        ),
    )
    p_open.set_defaults(func=_cmd_open)

    p_focus = subparsers.add_parser("focus", help="Activate the named panel within its group")
    p_focus.add_argument("ref", help="Panel ref")
    p_focus.set_defaults(func=_cmd_focus)

    p_split = subparsers.add_parser("split", help="Open a new panel as a split")
    p_split.add_argument(
        "target",
        help="Service name, ref, or https:// URL to open as the new panel",
    )
    p_split.add_argument(
        "--relative-to",
        default="self",
        help="Ref to split relative to. ``self`` (default) resolves to the caller's chat panel.",
    )
    p_split.add_argument("--direction", default="right", choices=_DIRECTIONS)
    p_split.add_argument("--ratio", type=float, default=0.6, help="Fraction the new panel occupies (0..1)")
    p_split.add_argument(
        "--new-group",
        action="store_true",
        help=(
            "Force a brand-new dockview group instead of tabbing into the group "
            "that already lives in the requested direction (the default)."
        ),
    )
    p_split.set_defaults(func=_cmd_split)

    p_close = subparsers.add_parser("close", help="Remove a panel")
    p_close.add_argument("ref", help="Panel ref")
    p_close.set_defaults(func=_cmd_close)

    p_move = subparsers.add_parser("move", help="Relocate an existing panel (state-preserving)")
    p_move.add_argument("ref", help="Panel ref to move")
    p_move.add_argument("--relative-to", required=True, help="Ref to move relative to")
    p_move.add_argument("--direction", required=True, choices=_DIRECTIONS)
    p_move.add_argument(
        "--new-group",
        action="store_true",
        help=(
            "Force a brand-new dockview group instead of moving the panel into "
            "an adjacent existing group (the default)."
        ),
    )
    p_move.set_defaults(func=_cmd_move)

    p_rename = subparsers.add_parser("rename", help="Update a panel's tab title")
    p_rename.add_argument("ref", help="Panel ref")
    p_rename.add_argument("title", help="New tab title")
    p_rename.set_defaults(func=_cmd_rename)

    p_max = subparsers.add_parser("maximize", help="Maximize a panel's group")
    p_max.add_argument("ref", help="Panel ref")
    p_max.set_defaults(func=_cmd_maximize)

    p_restore = subparsers.add_parser("restore", help="Exit a maximized group")
    p_restore.set_defaults(func=_cmd_restore)

    p_replace = subparsers.add_parser("replace-url", help="Swap an iframe's src")
    p_replace.add_argument("ref", help="Panel ref")
    p_replace.add_argument(
        "url",
        help="``service:<name>[/<path>]`` shorthand or a full https:// URL",
    )
    p_replace.set_defaults(func=_cmd_replace_url)

    p_refresh = subparsers.add_parser("refresh", help="Reload an iframe (or all iframes for a service)")
    p_refresh.add_argument("target", help="Panel ref. ``service:<name>`` reloads every iframe for that service.")
    p_refresh.set_defaults(func=_cmd_refresh)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
