"""Bootstrap service manager.

Reads services.toml, reconciles tmux windows to match, and watches for changes.

Each service defined in services.toml gets its own tmux window named svc-<name>.
When services.toml changes, new services are started and removed services are stopped.

Environment:
    Expects to run inside a tmux session (uses the current session name).
"""

import subprocess
import sys
import time
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]


SERVICES_FILE = Path("services.toml")
SVC_PREFIX = "svc-"
POLL_INTERVAL = 5  # seconds


def _get_session_name() -> str:
    """Get the current tmux session name."""
    result = subprocess.run(
        ["tmux", "display-message", "-p", "#S"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _list_managed_windows(session: str) -> dict[str, str]:
    """List tmux windows managed by bootstrap (prefixed with svc-).

    Returns {service_name: window_name}.
    """
    result = subprocess.run(
        ["tmux", "list-windows", "-t", session, "-F", "#{window_name}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {}

    windows = {}
    for name in result.stdout.strip().split("\n"):
        if name.startswith(SVC_PREFIX):
            service_name = name[len(SVC_PREFIX) :]
            windows[service_name] = name
    return windows


def _load_services() -> dict[str, dict]:
    """Load service definitions from services.toml.

    Returns {name: {command: str, restart: str}}.
    """
    if not SERVICES_FILE.exists():
        return {}

    with open(SERVICES_FILE, "rb") as f:
        data = tomllib.load(f)

    services = data.get("services", {})
    result = {}
    for name, config in services.items():
        if not isinstance(config, dict):
            continue
        command = config.get("command")
        if not command:
            continue
        result[name] = {
            "command": command,
            "restart": config.get("restart", "never"),
        }
    return result


def _start_service(session: str, name: str, command: str) -> None:
    """Start a service in a new tmux window."""
    window_name = f"{SVC_PREFIX}{name}"
    print(f"Starting service: {name} ({command})")
    subprocess.run(
        ["tmux", "new-window", "-t", session, "-n", window_name, "-d", command],
        check=False,
    )


def _stop_service(session: str, name: str) -> None:
    """Stop a service by killing its tmux window."""
    window_name = f"{SVC_PREFIX}{name}"
    print(f"Stopping service: {name}")
    subprocess.run(
        ["tmux", "kill-window", "-t", f"{session}:{window_name}"],
        check=False,
    )


def _get_file_mtime() -> float | None:
    """Get the modification time of services.toml, or None if it doesn't exist."""
    if not SERVICES_FILE.exists():
        return None
    return SERVICES_FILE.stat().st_mtime


def _reconcile(session: str, desired: dict[str, dict], current: dict[str, str]) -> None:
    """Reconcile the desired services with the currently running windows."""
    # Stop services that are no longer defined
    for name in current:
        if name not in desired:
            _stop_service(session, name)

    # Start services that are not running, or restart if command changed
    for name, config in desired.items():
        if name not in current:
            _start_service(session, name, config["command"])


def main() -> None:
    session = _get_session_name()
    if not session:
        print("Error: not running inside a tmux session", file=sys.stderr)
        sys.exit(1)

    print(f"Bootstrap service manager started (session: {session})")

    last_mtime = None

    while True:
        current_mtime = _get_file_mtime()

        # Reconcile on startup or when file changes
        if current_mtime != last_mtime:
            desired = _load_services()
            current = _list_managed_windows(session)
            _reconcile(session, desired, current)
            last_mtime = current_mtime

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
