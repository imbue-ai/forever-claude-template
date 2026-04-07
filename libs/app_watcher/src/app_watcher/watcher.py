"""Application watcher service.

Watches runtime/applications.toml for changes. On startup and on every change:
1. Reads the current applications from the TOML file
2. Queries the Cloudflare forwarding API for currently registered services
3. Diffs and reconciles (adds missing global=true apps, removes stale ones)
4. Writes the full set of server_registered / server_deregistered events
   to events/servers/events.jsonl

Uses both inotify (when available) and mtime polling (10-second fallback).
"""

import hashlib
import json
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

import httpx

APPLICATIONS_FILE = Path("runtime/applications.toml")
SECRETS_FILE = Path("runtime/secrets")
POLL_INTERVAL_SECONDS = 10
TOKEN_PATTERN = re.compile(
    r"""^export\s+CLOUDFLARE_TUNNEL_TOKEN=["']?([^"'\s]+)["']?\s*$""", re.MULTILINE
)


def _get_events_dir() -> Path | None:
    """Get the events directory from MNGR_AGENT_STATE_DIR."""
    state_dir = os.environ.get("MNGR_AGENT_STATE_DIR")
    if not state_dir:
        return None
    return Path(state_dir) / "events" / "servers"


def _read_tunnel_token() -> str | None:
    """Read the tunnel token from runtime/secrets."""
    if not SECRETS_FILE.exists():
        return None
    text = SECRETS_FILE.read_text()
    match = TOKEN_PATTERN.search(text)
    return match.group(1) if match else None


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


def _get_forwarding_url() -> str | None:
    """Get the Cloudflare forwarding API URL from environment."""
    return os.environ.get("CLOUDFLARE_FORWARDING_URL")


def _list_remote_services(
    forwarding_url: str, tunnel_token: str, tunnel_name: str
) -> list[dict[str, str]]:
    """Query the Cloudflare forwarding API for currently registered services."""
    try:
        response = httpx.get(
            f"{forwarding_url}/tunnels/{tunnel_name}/services",
            headers={"Authorization": f"Bearer {tunnel_token}"},
            timeout=15.0,
        )
        if response.status_code == 200:
            return response.json().get("services", [])
        print(
            f"[app-watcher] Failed to list remote services: {response.status_code}",
            file=sys.stderr,
            flush=True,
        )
    except httpx.HTTPError as e:
        print(
            f"[app-watcher] Error listing remote services: {e}",
            file=sys.stderr,
            flush=True,
        )
    return []


def _add_remote_service(
    forwarding_url: str, tunnel_token: str, tunnel_name: str, name: str, url: str
) -> None:
    """Add a service to the Cloudflare forwarding API."""
    try:
        response = httpx.post(
            f"{forwarding_url}/tunnels/{tunnel_name}/services",
            headers={"Authorization": f"Bearer {tunnel_token}"},
            json={"service_name": name, "service_url": url},
            timeout=15.0,
        )
        if response.status_code in (200, 201):
            print(
                f"[app-watcher] Registered service '{name}' globally",
                file=sys.stderr,
                flush=True,
            )
        else:
            print(
                f"[app-watcher] Failed to register service '{name}': {response.status_code} {response.text}",
                file=sys.stderr,
                flush=True,
            )
    except httpx.HTTPError as e:
        print(
            f"[app-watcher] Error registering service '{name}': {e}",
            file=sys.stderr,
            flush=True,
        )


def _remove_remote_service(
    forwarding_url: str, tunnel_token: str, tunnel_name: str, name: str
) -> None:
    """Remove a service from the Cloudflare forwarding API."""
    try:
        response = httpx.delete(
            f"{forwarding_url}/tunnels/{tunnel_name}/services/{name}",
            headers={"Authorization": f"Bearer {tunnel_token}"},
            timeout=15.0,
        )
        if response.status_code in (200, 204):
            print(
                f"[app-watcher] Deregistered service '{name}' globally",
                file=sys.stderr,
                flush=True,
            )
        else:
            print(
                f"[app-watcher] Failed to deregister service '{name}': {response.status_code}",
                file=sys.stderr,
                flush=True,
            )
    except httpx.HTTPError as e:
        print(
            f"[app-watcher] Error deregistering service '{name}': {e}",
            file=sys.stderr,
            flush=True,
        )


def _extract_tunnel_name_from_token(token: str) -> str | None:
    """Extract the tunnel name from the token by querying the API.

    The tunnel name is needed for API calls. We get it by listing tunnels
    and matching the tunnel_id from the token.
    """
    import base64

    try:
        decoded = json.loads(base64.b64decode(token))
        # Token format: {"a": account_id, "t": tunnel_id, "s": secret}
        # We can't derive the tunnel name from the token alone -- the
        # forwarding API accepts the token as auth and scopes to the tunnel.
        # The tunnel_name is embedded in the API path, but we need to
        # discover it. For now, use the tunnel_id as a lookup.
        return decoded.get("t", "")
    except (json.JSONDecodeError, ValueError):
        return None


def _reconcile_with_cloudflare(
    apps: list[dict[str, object]],
    forwarding_url: str,
    tunnel_token: str,
    tunnel_name: str,
) -> None:
    """Reconcile local applications.toml with cloudflare forwarding state."""
    # Get currently registered remote services
    remote_services = _list_remote_services(forwarding_url, tunnel_token, tunnel_name)
    remote_names = {s.get("service_name", "") for s in remote_services}

    # Determine which local apps want global forwarding
    desired_global: dict[str, str] = {}
    for app in apps:
        name = str(app.get("name", ""))
        url = str(app.get("url", ""))
        is_global = app.get("global", True)
        if name and url and is_global:
            desired_global[name] = url

    # Add missing services
    for name, url in desired_global.items():
        if name not in remote_names:
            _add_remote_service(forwarding_url, tunnel_token, tunnel_name, name, url)

    # Remove stale services (registered remotely but not desired locally)
    for name in remote_names:
        if name and name not in desired_global:
            _remove_remote_service(forwarding_url, tunnel_token, tunnel_name, name)


def _try_setup_inotify(path: Path) -> object | None:
    """Try to set up inotify on the applications file's parent directory."""
    try:
        import inotifyx  # type: ignore[import-untyped]

        fd = inotifyx.init()
        parent = path.parent
        parent.mkdir(parents=True, exist_ok=True)
        inotifyx.add_watch(
            fd,
            str(parent),
            inotifyx.IN_MODIFY | inotifyx.IN_CREATE | inotifyx.IN_MOVED_TO,
        )
        return fd
    except (ImportError, OSError):
        return None


def _wait_for_change_inotify(fd: object, timeout_seconds: float) -> bool:
    """Wait for an inotify event, with timeout."""
    try:
        import inotifyx  # type: ignore[import-untyped]

        events = inotifyx.get_events(fd, timeout_seconds)
        return len(events) > 0
    except (ImportError, OSError):
        return False


def main() -> None:
    """Main loop: watch applications.toml and reconcile with cloudflare."""
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

            # Reconcile with Cloudflare forwarding API
            forwarding_url = _get_forwarding_url()
            tunnel_token = _read_tunnel_token()
            if forwarding_url and tunnel_token:
                # The tunnel name for agent-scoped tokens is derived from the API
                # The bearer token auth scopes the request to the correct tunnel
                # so we just need any valid tunnel name -- the API resolves it from the token
                tunnel_name = "_"  # placeholder -- bearer auth identifies the tunnel
                _reconcile_with_cloudflare(
                    apps, forwarding_url, tunnel_token, tunnel_name
                )
            else:
                if not forwarding_url:
                    print(
                        "[app-watcher] CLOUDFLARE_FORWARDING_URL not set, skipping cloudflare sync",
                        file=sys.stderr,
                        flush=True,
                    )
                if not tunnel_token:
                    print(
                        "[app-watcher] No tunnel token found, skipping cloudflare sync",
                        file=sys.stderr,
                        flush=True,
                    )

            # Track current names for next diff
            previous_app_names = {str(a.get("name", "")) for a in apps if a.get("name")}

        # Wait for changes
        if inotify_fd is not None:
            _wait_for_change_inotify(inotify_fd, POLL_INTERVAL_SECONDS)
        else:
            time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
