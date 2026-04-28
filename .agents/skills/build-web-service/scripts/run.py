#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["tomlkit>=0.12"]
# ///
"""Stand up a new FastAPI web-service lib (and its services.toml entry).

Creates `libs/<package>/` with a sync-FastAPI starter, updates the root
pyproject.toml workspace/sources/dependencies, adds a `[services.<name>]`
entry, and runs `uv sync --all-packages` to materialize the workspace.

Usage:
    uv run .agents/skills/build-web-service/scripts/run.py \
        --name inbox-status --description "inbox status dashboard" \
        [--port 8081] [--extra-dep "jinja2>=3.1"] [--extra-dep "anthropic>=0.40"]

Run from the repo root (`/code`). Fails non-zero with a clear message on
any failure (lib already exists, reserved name, sync failure, etc.).
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable

import tomlkit
from tomlkit import TOMLDocument
from tomlkit.items import Array, Table

RESERVED_NAMES = frozenset(
    {
        "web",
        "web-server",
        "system-interface",
        "terminal",
        "cloudflared",
        "cloudflare-tunnel",
        "app-watcher",
        "bootstrap",
        "telegram-bot",
        "imbue-common",
    }
)
LOWEST_AUTO_PORT = 8081
KEBAB_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
LOCALHOST_PORT_RE = re.compile(r"http://localhost:(\d+)")


def _kebab_to_snake(name: str) -> str:
    return name.replace("-", "_")


def _validate_name(name: str) -> None:
    if not KEBAB_RE.match(name):
        sys.exit(
            f"error: --name {name!r} is not valid kebab-case "
            "(lowercase letters/digits with single hyphens, "
            "starting with a letter)"
        )
    if name in RESERVED_NAMES:
        sys.exit(f"error: --name {name!r} is reserved")


def _services_toml_ports(services_toml: Path) -> set[int]:
    if not services_toml.exists():
        return set()
    doc = tomlkit.parse(services_toml.read_text())
    services = doc.get("services", {})
    ports: set[int] = set()
    for entry in services.values():
        cmd = entry.get("command", "") if isinstance(entry, (dict, Table)) else ""
        for match in LOCALHOST_PORT_RE.finditer(str(cmd)):
            ports.add(int(match.group(1)))
    return ports


def _applications_toml_ports(applications_toml: Path) -> set[int]:
    if not applications_toml.exists():
        return set()
    doc = tomlkit.parse(applications_toml.read_text())
    apps = doc.get("applications", [])
    ports: set[int] = set()
    for app in apps:
        url = app.get("url", "")
        match = LOCALHOST_PORT_RE.search(str(url))
        if match:
            ports.add(int(match.group(1)))
    return ports


def _pick_port(repo_root: Path, requested: int | None) -> int:
    in_use = _services_toml_ports(repo_root / "services.toml") | _applications_toml_ports(
        repo_root / "runtime" / "applications.toml"
    )
    if requested is not None:
        if requested in in_use:
            sys.exit(f"error: --port {requested} is already in use by another service")
        return requested
    port = LOWEST_AUTO_PORT
    while port in in_use:
        port += 1
    return port


def _format_dep_list(extras: Iterable[str]) -> str:
    base = ['"fastapi>=0.110"', '"uvicorn>=0.29"']
    extras_lines = [f'"{dep}"' for dep in extras]
    all_lines = base + extras_lines
    return ",\n    ".join(all_lines)


def _lib_pyproject(name: str, package: str, description: str, extras: list[str]) -> str:
    deps_block = _format_dep_list(extras)
    return f"""[project]
name = "{name}"
version = "0.1.0"
description = "{description}"
readme = "README.md"
requires-python = ">=3.11"
dependencies = [
    {deps_block},
]

[project.scripts]
{name} = "{package}.runner:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/{package}"]
"""


def _lib_runner(name: str, description: str, port: int) -> str:
    return f'''"""{description}.

Services run from /code (the repo root). Conventions:

- Runtime state files (anything written and read across runs, e.g.
  cursors, caches, last-visit timestamps): use cwd-relative paths like
  ``Path("runtime/{name}/...")``. Do NOT use ``Path(__file__)``-based
  paths for runtime state -- the bug to avoid is one process writing
  to ``/code/runtime/...`` while another reads from
  ``/code/libs/<pkg>/runtime/...``.
- Static assets shipped alongside this file (templates, default
  configs, bundled JSON): ``Path(__file__).parent / "assets/..."`` is
  fine and is the right pattern.
"""

import os

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

WEB_SERVER_PORT = int(os.environ.get("WEB_SERVER_PORT", "{port}"))

app = FastAPI(title="{name}")


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html><body>"
        "<h1>{name}</h1>"
        "<p>{description}</p>"
        "</body></html>"
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {{"status": "ok"}}


def main() -> None:
    uvicorn.run(app, host="127.0.0.1", port=WEB_SERVER_PORT)


if __name__ == "__main__":
    main()
'''


def _lib_ratchets() -> str:
    return '''from pathlib import Path

from imbue.imbue_common.ratchet_testing import standard_ratchet_checks as rc
from imbue.imbue_common.ratchet_testing.ratchets import check_no_ruff_errors
from inline_snapshot import snapshot

_DIR = Path(__file__).parent


# --- Code safety ---


def test_prevent_todos() -> None:
    rc.check_todos(_DIR, snapshot(0))


def test_prevent_exec_usage() -> None:
    rc.check_exec(_DIR, snapshot(0))


def test_prevent_eval_usage() -> None:
    rc.check_eval(_DIR, snapshot(0))


def test_prevent_while_true() -> None:
    rc.check_while_true(_DIR, snapshot(0))


def test_prevent_time_sleep() -> None:
    rc.check_time_sleep(_DIR, snapshot(0))


def test_prevent_global_keyword() -> None:
    rc.check_global_keyword(_DIR, snapshot(0))


def test_prevent_bare_print() -> None:
    rc.check_bare_print(_DIR, snapshot(0))


# --- Exception handling ---


def test_prevent_bare_except() -> None:
    rc.check_bare_except(_DIR, snapshot(0))


def test_prevent_broad_exception_catch() -> None:
    rc.check_broad_exception_catch(_DIR, snapshot(0))


def test_prevent_builtin_exception_raises() -> None:
    rc.check_builtin_exception_raises(_DIR, snapshot(0))


# --- Import style ---


def test_prevent_inline_imports() -> None:
    rc.check_inline_imports(_DIR, snapshot(0))


def test_prevent_relative_imports() -> None:
    rc.check_relative_imports(_DIR, snapshot(0))


# --- Banned libraries and patterns ---


def test_prevent_asyncio_import() -> None:
    rc.check_asyncio_import(_DIR, snapshot(0))


def test_prevent_dataclasses_import() -> None:
    rc.check_dataclasses_import(_DIR, snapshot(0))


# --- Linting ---


def test_no_ruff_errors() -> None:
    check_no_ruff_errors(_DIR)
'''


def _lib_readme(name: str, description: str) -> str:
    return f"# {name}\n\n{description}\n"


def _write_lib(repo_root: Path, name: str, description: str, port: int, extras: list[str]) -> Path:
    package = _kebab_to_snake(name)
    lib_dir = repo_root / "libs" / package
    if lib_dir.exists():
        sys.exit(f"error: {lib_dir} already exists")
    src_dir = lib_dir / "src" / package
    src_dir.mkdir(parents=True)
    (lib_dir / "pyproject.toml").write_text(_lib_pyproject(name, package, description, extras))
    (lib_dir / "README.md").write_text(_lib_readme(name, description))
    (lib_dir / f"test_{package}_ratchets.py").write_text(_lib_ratchets())
    (src_dir / "__init__.py").write_text("")
    (src_dir / "runner.py").write_text(_lib_runner(name, description, port))
    return lib_dir


def _ensure_in_array(array: Array, value: str) -> bool:
    """Append value to a TOML array if missing. Returns True if appended."""
    for item in array:
        if str(item) == value:
            return False
    array.append(value)
    return True


def _update_root_pyproject(repo_root: Path, name: str, package: str) -> None:
    path = repo_root / "pyproject.toml"
    doc: TOMLDocument = tomlkit.parse(path.read_text())

    project = doc.get("project")
    if not isinstance(project, Table):
        sys.exit("error: root pyproject.toml is missing a [project] table")
    deps = project.get("dependencies")
    if not isinstance(deps, Array):
        sys.exit("error: root pyproject.toml [project].dependencies is missing or not an array")
    _ensure_in_array(deps, name)

    tool = doc.get("tool")
    if not isinstance(tool, Table):
        sys.exit("error: root pyproject.toml is missing a [tool] table")
    uv = tool.get("uv")
    if not isinstance(uv, Table):
        sys.exit("error: root pyproject.toml is missing [tool.uv]")
    workspace = uv.get("workspace")
    if not isinstance(workspace, Table):
        sys.exit("error: root pyproject.toml is missing [tool.uv.workspace]")
    members = workspace.get("members")
    if not isinstance(members, Array):
        sys.exit("error: [tool.uv.workspace].members is missing or not an array")
    _ensure_in_array(members, f"libs/{package}")

    sources = uv.get("sources")
    if not isinstance(sources, Table):
        sys.exit("error: root pyproject.toml is missing [tool.uv.sources]")
    if name not in sources:
        source_entry = tomlkit.inline_table()
        source_entry["workspace"] = True
        sources[name] = source_entry

    path.write_text(tomlkit.dumps(doc))


def _update_services_toml(repo_root: Path, name: str, port: int) -> None:
    path = repo_root / "services.toml"
    doc: TOMLDocument
    if path.exists():
        doc = tomlkit.parse(path.read_text())
    else:
        doc = tomlkit.document()
    services = doc.get("services")
    if services is None:
        services = tomlkit.table()
        doc["services"] = services
    if not isinstance(services, Table):
        sys.exit("error: services.toml [services] is not a table")
    if name in services:
        sys.exit(f"error: services.toml already has a [services.{name}] entry")

    entry = tomlkit.table()
    entry["command"] = (
        f"python3 scripts/forward_port.py --url http://localhost:{port} "
        f"--name {name} && uv run {name}"
    )
    entry["restart"] = "on-failure"
    services[name] = entry

    path.write_text(tomlkit.dumps(doc))


def _run_uv_sync(repo_root: Path) -> None:
    result = subprocess.run(
        ["uv", "sync", "--all-packages"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        sys.exit(f"error: `uv sync --all-packages` failed (exit {result.returncode})")


def _find_repo_root(start: Path) -> Path:
    current = start.resolve()
    for parent in [current, *current.parents]:
        if (parent / "pyproject.toml").exists() and (parent / "services.toml").exists():
            return parent
    sys.exit("error: could not locate repo root (pyproject.toml + services.toml)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--name", required=True, help="kebab-case service name")
    parser.add_argument("--description", required=True, help="one-line description")
    parser.add_argument("--port", type=int, default=None, help="explicit port (auto-picked if omitted)")
    parser.add_argument(
        "--extra-dep",
        action="append",
        default=[],
        help="additional pip dep beyond fastapi/uvicorn (repeatable)",
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="repo root (defaults to nearest ancestor containing pyproject.toml + services.toml)",
    )
    parser.add_argument(
        "--skip-uv-sync",
        action="store_true",
        help="skip running `uv sync --all-packages` after generation (for tests/dry runs)",
    )
    args = parser.parse_args()

    _validate_name(args.name)
    repo_root = (
        Path(args.repo_root).resolve() if args.repo_root else _find_repo_root(Path.cwd())
    )
    package = _kebab_to_snake(args.name)
    port = _pick_port(repo_root, args.port)

    lib_dir = _write_lib(repo_root, args.name, args.description, port, list(args.extra_dep))
    _update_root_pyproject(repo_root, args.name, package)
    _update_services_toml(repo_root, args.name, port)

    if not args.skip_uv_sync:
        _run_uv_sync(repo_root)

    print(
        f"Created lib at {lib_dir.relative_to(repo_root)} "
        f"(service `{args.name}` on port {port}). "
        f"Next: implement your routes in src/{package}/runner.py, "
        f"then `uv run {args.name}` to start (or let the bootstrap manager pick up "
        f"the new services.toml entry)."
    )


if __name__ == "__main__":
    main()
