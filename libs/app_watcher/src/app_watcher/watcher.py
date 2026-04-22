"""Application watcher service.

Watches runtime/applications.toml for changes. On startup and on every change,
writes service_registered / service_deregistered events to
events/services/events.jsonl so the desktop client can discover available services.

Uses both inotify (when available) and mtime polling (10-second fallback).
"""

import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from imbue.imbue_common.event_envelope import (
    EventEnvelope,
    EventId,
    EventSource,
    EventType,
    IsoTimestamp,
)

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

APPLICATIONS_FILE = Path("runtime/applications.toml")
POLL_INTERVAL_SECONDS = 10

_EVENT_SOURCE = EventSource("services")
_EVENT_TYPE_REGISTERED = EventType("service_registered")
_EVENT_TYPE_DEREGISTERED = EventType("service_deregistered")


class ServiceRegisteredEvent(EventEnvelope):
    """A service registered its URL with the agent."""

    service: str
    url: str


class ServiceDeregisteredEvent(EventEnvelope):
    """A service that was previously registered is no longer available."""

    service: str


def _new_event_id() -> EventId:
    return EventId(f"evt-{uuid4().hex}")


def _now_iso() -> IsoTimestamp:
    return IsoTimestamp(
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    )


def _get_events_dir() -> Path | None:
    """Get the events directory from MNGR_AGENT_STATE_DIR."""
    state_dir = os.environ.get("MNGR_AGENT_STATE_DIR")
    if not state_dir:
        return None
    return Path(state_dir) / "events" / "services"


def _load_applications() -> list[dict[str, object]]:
    """Load applications from the TOML file."""
    if not APPLICATIONS_FILE.exists():
        return []
    with open(APPLICATIONS_FILE, "rb") as f:
        data = tomllib.load(f)
    return data.get("applications", [])


def _write_events(
    events_dir: Path,
    current_apps: list[dict[str, object]],
    previous_app_names: set[str],
) -> None:
    """Write service events for all current applications and deregistration events for removed ones."""
    events_dir.mkdir(parents=True, exist_ok=True)
    events_path = events_dir / "events.jsonl"

    current_names: set[str] = set()

    with open(events_path, "a") as f:
        for app in current_apps:
            name = str(app.get("name", ""))
            url = str(app.get("url", ""))
            if not name or not url:
                continue
            current_names.add(name)
            event = ServiceRegisteredEvent(
                timestamp=_now_iso(),
                type=_EVENT_TYPE_REGISTERED,
                event_id=_new_event_id(),
                source=_EVENT_SOURCE,
                service=name,
                url=url,
            )
            f.write(event.model_dump_json() + "\n")

        for name in sorted(previous_app_names - current_names):
            event = ServiceDeregisteredEvent(
                timestamp=_now_iso(),
                type=_EVENT_TYPE_DEREGISTERED,
                event_id=_new_event_id(),
                source=_EVENT_SOURCE,
                service=name,
            )
            f.write(event.model_dump_json() + "\n")


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
    """Main loop: watch applications.toml and write service events."""
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

            # Write service events
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
