"""Application watcher service.

Watches runtime/applications.toml for changes. On startup and on every change,
writes server_registered / server_deregistered events to
events/servers/events.jsonl so the desktop client can discover available servers.

Uses both inotify (when available) and mtime polling (10-second fallback).
"""

import hashlib
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

APPLICATIONS_FILE = Path("runtime/applications.toml")
POLL_INTERVAL_SECONDS = 10


def _get_events_dir() -> Path | None:
    """Get the events directory from MNGR_AGENT_STATE_DIR."""
    state_dir = os.environ.get("MNGR_AGENT_STATE_DIR")
    if not state_dir:
        return None
    return Path(state_dir) / "events" / "servers"


def _load_applications() -> list[dict[str, object]]:
    """Load applications from the TOML file."""
    if not APPLICATIONS_FILE.exists():
        return []
    with open(APPLICATIONS_FILE, "rb") as f:
        data = tomllib.load(f)
    return data.get("applications", [])


def _make_event_id(server: str, url: str) -> str:
    """Generate a deterministic event ID from server name and URL."""
    raw = f"{server}:{url}"
    return "evt-" + hashlib.sha256(raw.encode()).hexdigest()[:32]


def _write_events(
    events_dir: Path,
    current_apps: list[dict[str, object]],
    previous_app_names: set[str],
) -> None:
    """Write server events for all current applications and deregistration events for removed ones."""
    events_dir.mkdir(parents=True, exist_ok=True)
    events_path = events_dir / "events.jsonl"

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    current_names = set()

    with open(events_path, "a") as f:
        # Write registration events for all current applications
        for app in current_apps:
            name = str(app.get("name", ""))
            url = str(app.get("url", ""))
            if not name or not url:
                continue
            current_names.add(name)
            event = {
                "timestamp": timestamp,
                "type": "server_registered",
                "event_id": _make_event_id(name, url),
                "source": "servers",
                "server": name,
                "url": url,
            }
            f.write(json.dumps(event) + "\n")

        # Write deregistration events for removed applications
        removed = previous_app_names - current_names
        for name in sorted(removed):
            event = {
                "timestamp": timestamp,
                "type": "server_deregistered",
                "event_id": _make_event_id(name, "removed"),
                "source": "servers",
                "server": name,
            }
            f.write(json.dumps(event) + "\n")


def _try_setup_inotify(path: Path) -> object | None:
    """Try to set up inotify on the applications file's parent directory.

    Uses inotify_simple (pure Python, Linux only). Returns the INotify
    instance on success, or None on non-Linux platforms or errors.
    """
    try:
        from inotify_simple import INotify
        from inotify_simple import flags as inotify_flags

        inotify = INotify()
        parent = path.parent
        parent.mkdir(parents=True, exist_ok=True)
        inotify.add_watch(
            str(parent),
            inotify_flags.MODIFY | inotify_flags.CREATE | inotify_flags.MOVED_TO,
        )
        return inotify
    except (ImportError, OSError):
        return None


def _wait_for_change_inotify(inotify: object, timeout_seconds: float) -> bool:
    """Wait for an inotify event, with timeout."""
    try:
        from inotify_simple import INotify

        if not isinstance(inotify, INotify):
            return False
        timeout_ms = int(timeout_seconds * 1000)
        events = inotify.read(timeout=timeout_ms)
        return len(events) > 0
    except (ImportError, OSError):
        return False


def main() -> None:
    """Main loop: watch applications.toml and write server events."""
    print("[app-watcher] Starting application watcher", file=sys.stderr, flush=True)

    APPLICATIONS_FILE.parent.mkdir(parents=True, exist_ok=True)

    events_dir = _get_events_dir()
    if events_dir is None:
        print(
            "[app-watcher] WARNING: MNGR_AGENT_STATE_DIR not set, events will not be written",
            file=sys.stderr,
            flush=True,
        )

    inotify_fd = _try_setup_inotify(APPLICATIONS_FILE)
    if inotify_fd is not None:
        print(
            "[app-watcher] Using inotify for file watching", file=sys.stderr, flush=True
        )
    else:
        print(
            "[app-watcher] inotify not available, using polling only",
            file=sys.stderr,
            flush=True,
        )

    last_mtime: float = 0.0
    previous_app_names: set[str] = set()

    def _handle_signal(signum: int, frame: object) -> None:
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    while True:
        try:
            new_mtime = (
                APPLICATIONS_FILE.stat().st_mtime if APPLICATIONS_FILE.exists() else 0.0
            )
        except OSError:
            new_mtime = 0.0

        if new_mtime != last_mtime:
            last_mtime = new_mtime
            apps = _load_applications()

            print(
                f"[app-watcher] Applications changed: {[a.get('name') for a in apps]}",
                file=sys.stderr,
                flush=True,
            )

            # Write server events
            if events_dir is not None:
                _write_events(events_dir, apps, previous_app_names)

            # Track current names for next diff
            previous_app_names = {str(a.get("name", "")) for a in apps if a.get("name")}

        # Wait for changes
        if inotify_fd is not None:
            _wait_for_change_inotify(inotify_fd, POLL_INTERVAL_SECONDS)
        else:
            time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
