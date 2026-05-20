#!/usr/bin/env python3
"""Register or remove an application port in runtime/applications.toml.

Uses file locking to safely upsert or remove entries. Called by services
on startup to declare the ports they expose.

Usage:
    python3 scripts/forward_port.py --name web --url http://localhost:8080
    python3 scripts/forward_port.py --remove --name web
"""

import argparse
import fcntl
import os
import tempfile
from pathlib import Path

import tomlkit

DEFAULT_APPLICATIONS_FILE = "runtime/applications.toml"
ENV_APPLICATIONS_FILE = "MINDS_APPLICATIONS_FILE"


def _applications_file() -> Path:
    """Path to the agent's applications.toml registry.

    Defaults to ``runtime/applications.toml`` relative to cwd. Override
    via ``MINDS_APPLICATIONS_FILE`` -- used by tests and by callers that
    need to point at a non-default registry (e.g. when running outside
    the agent's repo root). Mirrors ``scripts/layout.py``.
    """
    return Path(os.environ.get(ENV_APPLICATIONS_FILE, DEFAULT_APPLICATIONS_FILE))


def _load_applications(path: Path) -> tomlkit.TOMLDocument:
    if not path.exists():
        doc = tomlkit.document()
        doc.add("applications", tomlkit.aot())
        return doc
    with open(path, "rb") as f:
        return tomlkit.load(f)


def _save_applications(path: Path, doc: tomlkit.TOMLDocument) -> None:
    # Atomic write: write to a temp file in the same directory, then os.replace()
    # into place. This guarantees that readers (like app-watcher) never observe
    # a truncated/partial file during the write window.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=path.parent, prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w") as f:
            tomlkit.dump(doc, f)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _upsert(path: Path, name: str, url: str) -> None:
    doc = _load_applications(path)
    apps = doc.get("applications", [])

    # Find existing entry by name
    for app in apps:
        if app.get("name") == name:
            app["url"] = url
            _save_applications(path, doc)
            return

    # No existing entry -- append
    entry = tomlkit.table()
    entry.add("name", name)
    entry.add("url", url)
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
        "--remove",
        action="store_true",
        help="Remove the named application instead of adding it",
    )
    args = parser.parse_args()

    if not args.remove and not args.url:
        parser.error("--url is required when not using --remove")

    applications_file = _applications_file()
    lock_path = applications_file.parent / ".applications.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            if args.remove:
                _remove(applications_file, args.name)
            else:
                _upsert(applications_file, args.name, args.url)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


if __name__ == "__main__":
    main()
