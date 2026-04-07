#!/usr/bin/env python3
"""Register or remove an application port in runtime/applications.toml.

Uses file locking to safely upsert or remove entries. Called by services
on startup to declare the ports they expose.

Usage:
    python3 scripts/forward_port.py --name web --url http://localhost:8080
    python3 scripts/forward_port.py --name web --url http://localhost:8080 --no-global
    python3 scripts/forward_port.py --remove --name web
"""

import argparse
import fcntl
from pathlib import Path

import tomlkit

APPLICATIONS_FILE = Path("runtime/applications.toml")


def _load_applications(path: Path) -> tomlkit.TOMLDocument:
    if not path.exists():
        doc = tomlkit.document()
        doc.add("applications", tomlkit.aot())
        return doc
    with open(path, "rb") as f:
        return tomlkit.load(f)


def _save_applications(path: Path, doc: tomlkit.TOMLDocument) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        tomlkit.dump(doc, f)


def _upsert(path: Path, name: str, url: str, global_forward: bool) -> None:
    doc = _load_applications(path)
    apps = doc.get("applications", [])

    # Find existing entry by name
    for app in apps:
        if app.get("name") == name:
            app["url"] = url
            app["global"] = global_forward
            _save_applications(path, doc)
            return

    # No existing entry -- append
    entry = tomlkit.table()
    entry.add("name", name)
    entry.add("url", url)
    entry.add("global", global_forward)
    apps.append(entry)
    _save_applications(path, doc)


def _remove(path: Path, name: str) -> None:
    if not path.exists():
        return
    doc = _load_applications(path)
    apps = doc.get("applications", [])
    original_len = len(apps)

    # Remove matching entries
    to_remove = [i for i, app in enumerate(apps) if app.get("name") == name]
    for i in reversed(to_remove):
        del apps[i]

    if len(apps) != original_len:
        _save_applications(path, doc)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Register or remove an application port"
    )
    parser.add_argument(
        "--name", required=True, help="Application name (e.g. 'web', 'terminal')"
    )
    parser.add_argument(
        "--url",
        help="Full URL where the application is accessible (e.g. http://localhost:8080)",
    )
    parser.add_argument(
        "--global",
        dest="global_forward",
        action="store_true",
        default=True,
        help="Enable global cloudflare forwarding (default: true)",
    )
    parser.add_argument(
        "--no-global",
        dest="global_forward",
        action="store_false",
        help="Disable global cloudflare forwarding",
    )
    parser.add_argument(
        "--remove",
        action="store_true",
        help="Remove the named application instead of adding it",
    )
    args = parser.parse_args()

    if not args.remove and not args.url:
        parser.error("--url is required when not using --remove")

    lock_path = APPLICATIONS_FILE.parent / ".applications.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            if args.remove:
                _remove(APPLICATIONS_FILE, args.name)
            else:
                _upsert(APPLICATIONS_FILE, args.name, args.url, args.global_forward)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


if __name__ == "__main__":
    main()
