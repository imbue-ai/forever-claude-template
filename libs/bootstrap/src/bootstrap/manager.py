"""Bootstrap service manager.

Reads services.toml, reconciles tmux windows to match, and watches for changes.

Each service defined in services.toml gets its own tmux window named svc-<name>.
When services.toml changes, new services are started, removed services are
stopped, and services whose `command` changed are restarted.

Environment:
    Expects to run inside a tmux session (uses the current session name).
"""

import subprocess
import sys
import time
from pathlib import Path

from loguru import logger

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]


SERVICES_FILE = Path("services.toml")
SVC_PREFIX = "svc-"
# Tmux window user-option used to remember the command we started a service
# with, so we can detect command edits in services.toml on the next reconcile.
SVC_COMMAND_OPTION = "@svc_command"
POLL_INTERVAL = 5  # seconds


def _get_session_name() -> str:
    """Get the current tmux session name."""
    result = subprocess.run(
        ["tmux", "display-message", "-p", "#S"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _list_managed_windows(session: str) -> dict[str, dict[str, str]]:
    """List tmux windows managed by bootstrap (prefixed with svc-).

    Returns {service_name: {"window_name": str, "command": str}}. The command
    is read from the per-window user-option SVC_COMMAND_OPTION that we set when
    starting the service; it is the empty string if the option is unset (e.g.
    the window was created by an older manager that did not record it).
    """
    result = subprocess.run(
        ["tmux", "list-windows", "-t", session, "-F", "#{window_name}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {}

    windows: dict[str, dict[str, str]] = {}
    for name in result.stdout.strip().split("\n"):
        if not name.startswith(SVC_PREFIX):
            continue
        service_name = name[len(SVC_PREFIX) :]
        command = _get_window_command(session, name)
        windows[service_name] = {"window_name": name, "command": command}
    return windows


def _get_window_command(session: str, window_name: str) -> str:
    """Read the recorded service command from a managed window's user-option."""
    target = f"{session}:{window_name}"
    result = subprocess.run(
        ["tmux", "show-options", "-t", target, "-w", "-v", SVC_COMMAND_OPTION],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.rstrip("\n")


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
    """Start a service in a new tmux window.

    Creates the window without a command so it uses the session's default-command
    (which sources env files), then sends the service command via send-keys.
    This ensures the service process inherits MNGR_AGENT_STATE_DIR and other
    agent environment variables. Records the command on the window via a user
    option so subsequent reconciles can detect command edits.
    """
    window_name = f"{SVC_PREFIX}{name}"
    window_target = f"{session}:{window_name}"
    logger.info("Starting service: {} ({})", name, command)
    subprocess.run(
        ["tmux", "new-window", "-t", session, "-n", window_name, "-d"],
        check=False,
    )
    subprocess.run(
        ["tmux", "set-option", "-t", window_target, "-w", SVC_COMMAND_OPTION, command],
        check=False,
    )
    subprocess.run(
        ["tmux", "send-keys", "-t", window_target, command, "Enter"],
        check=False,
    )


def _stop_service(session: str, name: str) -> None:
    """Stop a service by killing its tmux window."""
    window_name = f"{SVC_PREFIX}{name}"
    logger.info("Stopping service: {}", name)
    subprocess.run(
        ["tmux", "kill-window", "-t", f"{session}:{window_name}"],
        check=False,
    )


def _get_file_mtime() -> float | None:
    """Get the modification time of services.toml, or None if it doesn't exist."""
    if not SERVICES_FILE.exists():
        return None
    return SERVICES_FILE.stat().st_mtime


def _compute_actions(
    desired: dict[str, dict],
    current: dict[str, dict[str, str]],
) -> tuple[list[str], list[tuple[str, str]]]:
    """Compute (stops, starts) needed to make `current` match `desired`.

    A service whose command changed appears in both lists -- it must be stopped
    and then started again. Pure function so it can be unit-tested without
    invoking tmux.
    """
    stops: list[str] = []
    starts: list[tuple[str, str]] = []

    for name, running in current.items():
        if name not in desired:
            stops.append(name)
        elif running["command"] != desired[name]["command"]:
            stops.append(name)

    for name, config in desired.items():
        if name not in current:
            starts.append((name, config["command"]))
        elif current[name]["command"] != config["command"]:
            starts.append((name, config["command"]))

    return stops, starts


def _reconcile(session: str, desired: dict[str, dict], current: dict[str, dict[str, str]]) -> None:
    """Reconcile the desired services with the currently running windows."""
    stops, starts = _compute_actions(desired, current)
    for name in stops:
        _stop_service(session, name)
    for name, command in starts:
        _start_service(session, name, command)


def main() -> None:
    session = _get_session_name()
    if not session:
        logger.error("Not running inside a tmux session")
        sys.exit(1)

    logger.info("Bootstrap service manager started (session: {})", session)

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
