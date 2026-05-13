"""Bootstrap service manager.

Reads services.toml, reconciles tmux windows to match, and watches for changes.

Each service defined in services.toml gets its own tmux window named svc-<name>.
When services.toml changes, new services are started, removed services are
stopped, and services whose `command` changed are restarted.

Before reconciling services for the first time, runs a one-time pre-services
init step that ensures runtime/ exists as a git worktree of the per-agent
backup branch (mindsbackup/$MNGR_AGENT_ID), so any service that subsequently
writes into runtime/ (cloudflared, app-watcher, telegram, etc.) does so
inside that worktree.

Environment:
    Expects to run inside a tmux session (uses the current session name).
"""

import json
import os
import shutil
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

RUNTIME_DIR = Path("runtime")
RUNTIME_PREEXISTING_DIR = Path("runtime.preexisting")
RUNTIME_BACKUP_USER_NAME = "runtime-backup"
RUNTIME_BACKUP_USER_EMAIL = "runtime-backup@mindsbackup.local"

# Sentinel file recording that we've already created the user-facing
# ``assistant`` chat agent on this workspace. Lives inside runtime/ so it
# rides along with the runtime-backup branch and survives container loss.
# A user who deliberately ``mngr destroy``'s the assistant will not see it
# re-created -- they'd need to delete this file and restart bootstrap.
ASSISTANT_SENTINEL = RUNTIME_DIR / ".assistant-created"
ASSISTANT_AGENT_NAME = "assistant"
ASSISTANT_WELCOME_MESSAGE = "/welcome"


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
    and then started again.
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


def _reconcile(
    session: str, desired: dict[str, dict], current: dict[str, dict[str, str]]
) -> None:
    """Reconcile the desired services with the currently running windows."""
    stops, starts = _compute_actions(desired, current)
    for name in stops:
        _stop_service(session, name)
    for name, command in starts:
        _start_service(session, name, command)


def _git_main(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command in the main checkout, never raising."""
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False)


def _git_runtime(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command inside the runtime worktree, never raising."""
    return subprocess.run(
        ["git", "-C", str(RUNTIME_DIR), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _restore_preexisting_into_worktree() -> None:
    """Move any files from runtime.preexisting/ back into runtime/."""
    if not RUNTIME_PREEXISTING_DIR.exists():
        return
    for entry in list(RUNTIME_PREEXISTING_DIR.iterdir()):
        target = RUNTIME_DIR / entry.name
        if target.exists():
            # Don't clobber what the worktree already has (e.g. a fresh
            # .gitignore we just wrote).
            continue
        shutil.move(str(entry), str(target))
    try:
        RUNTIME_PREEXISTING_DIR.rmdir()
    except OSError:
        logger.warning(
            "{} not empty after restore; leaving for inspection",
            RUNTIME_PREEXISTING_DIR,
        )


def _stage_preexisting_aside() -> None:
    """Move runtime/'s contents to runtime.preexisting/ so we can add a worktree.

    Only called when runtime/ exists with files but is not yet a git worktree.
    """
    if RUNTIME_PREEXISTING_DIR.exists():
        # Stale leftover from a prior failed init -- clear it.
        shutil.rmtree(RUNTIME_PREEXISTING_DIR)
    shutil.move(str(RUNTIME_DIR), str(RUNTIME_PREEXISTING_DIR))


def _runtime_dir_has_files() -> bool:
    """Return True if runtime/ exists and contains anything."""
    if not RUNTIME_DIR.exists():
        return False
    return any(RUNTIME_DIR.iterdir())


def _init_runtime_worktree() -> None:
    """One-time setup of runtime/ as a worktree of mindsbackup/$MNGR_AGENT_ID.

    Best-effort: logs and returns rather than raising on any failure, so a
    transient git problem does not prevent other services from starting.
    """
    agent_id = os.environ.get("MNGR_AGENT_ID")
    if not agent_id:
        logger.warning(
            "MNGR_AGENT_ID is unset; skipping runtime worktree init "
            "(runtime-backup service will also no-op)"
        )
        return

    branch = f"mindsbackup/{agent_id}"

    if (RUNTIME_DIR / ".git").exists():
        logger.info("runtime/ is already a worktree; skipping init")
        return

    logger.info("Initializing runtime worktree on branch {}", branch)

    # Best-effort fetch so we can detect a pre-existing remote branch (e.g.
    # restored after a container restart on the same agent id).
    fetch_result = _git_main("fetch", "origin", branch)
    remote_ref = f"origin/{branch}"
    has_remote = (
        fetch_result.returncode == 0
        and _git_main("rev-parse", "--verify", remote_ref).returncode == 0
    )

    staged_aside = False
    if _runtime_dir_has_files():
        logger.warning(
            "runtime/ already has files; staging them aside before adding the worktree"
        )
        _stage_preexisting_aside()
        staged_aside = True

    if has_remote:
        result = _git_main(
            "worktree", "add", "-B", branch, str(RUNTIME_DIR), remote_ref
        )
    else:
        result = _git_main(
            "worktree", "add", "--orphan", "-b", branch, str(RUNTIME_DIR)
        )

    if result.returncode != 0:
        logger.error(
            "git worktree add failed (rc={}): {}",
            result.returncode,
            result.stderr.strip(),
        )
        # Restore preexisting files so other services don't lose them.
        if staged_aside:
            if not RUNTIME_DIR.exists():
                shutil.move(str(RUNTIME_PREEXISTING_DIR), str(RUNTIME_DIR))
            else:
                _restore_preexisting_into_worktree()
        return

    # Configure bot identity for backup commits inside this worktree only.
    _git_runtime("config", "user.name", RUNTIME_BACKUP_USER_NAME)
    _git_runtime("config", "user.email", RUNTIME_BACKUP_USER_EMAIL)

    if has_remote:
        # Make sure the local branch tracks the remote (some git versions
        # don't set this automatically with -B + an explicit ref).
        _git_runtime("branch", "--set-upstream-to", remote_ref)
    else:
        # Fresh orphan branch: write the .gitignore for secrets and make an
        # initial empty commit so push has something to push.
        gitignore = RUNTIME_DIR / ".gitignore"
        gitignore.write_text("secrets\n")
        _git_runtime("add", ".gitignore")
        commit = _git_runtime("commit", "--allow-empty", "-m", "runtime backup: init")
        if commit.returncode != 0:
            logger.error(
                "initial commit failed (rc={}): {}",
                commit.returncode,
                commit.stderr.strip(),
            )

    if staged_aside:
        _restore_preexisting_into_worktree()

    if os.environ.get("GH_TOKEN"):
        if has_remote:
            push = _git_runtime("push")
        else:
            push = _git_runtime("push", "--set-upstream", "origin", branch)
        if push.returncode != 0:
            logger.warning(
                "initial push failed (rc={}): {} (runtime-backup service will retry)",
                push.returncode,
                push.stderr.strip(),
            )
    else:
        logger.info("No GH_TOKEN; skipping initial push")


def _read_own_agent_labels() -> dict[str, str]:
    """Return the bootstrap agent's mngr labels via ``mngr list --format json``.

    Returns an empty dict on any failure -- assistant creation falls back
    to no inherited labels in that case, which is fine for the happy-path
    case (the user can still chat with the assistant; only label-driven
    sub-agent filtering will miss it).
    """
    agent_id = os.environ.get("MNGR_AGENT_ID", "")
    if not agent_id:
        return {}
    result = subprocess.run(
        ["mngr", "list", "--format", "json", "--include", f'id == "{agent_id}"'],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        logger.warning(
            "mngr list for own agent labels failed: {}", result.stderr.strip()
        )
        return {}
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        logger.warning("Could not parse own-agent label JSON: {}", e)
        return {}
    agents: list[dict[str, object]] = []
    if isinstance(data, dict) and "agents" in data:
        agents = data["agents"]
    elif isinstance(data, list):
        agents = data
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        if agent.get("id") != agent_id:
            continue
        labels = agent.get("labels", {})
        if isinstance(labels, dict):
            return {str(k): str(v) for k, v in labels.items()}
    return {}


def _ensure_assistant_agent() -> None:
    """Create the user-facing ``assistant`` chat agent if one doesn't exist.

    Idempotent: gated on the ``runtime/.assistant-created`` sentinel file.
    Runs once per workspace lifetime; on container restarts the sentinel
    is preserved by the runtime-backup branch, so this is effectively a
    "first-ever-boot" hook. A user who deliberately ``mngr destroy``'s
    the assistant won't see it re-created -- the sentinel is sticky.

    Inherits ``workspace`` and ``project`` labels from the system-services
    agent so the assistant is discoverable by the same CEL filters used
    for chat-agent listing today.

    Best-effort: any failure logs and continues -- bootstrap's primary
    responsibility is service reconciliation, not chat-agent creation,
    so a transient ``mngr create`` failure should not prevent
    ``svc-system_interface`` etc. from coming up.
    """
    if ASSISTANT_SENTINEL.exists():
        logger.info("Assistant sentinel present; skipping create")
        return

    inherited_labels = _read_own_agent_labels()
    workspace_label = inherited_labels.get("workspace", "")
    project_label = inherited_labels.get("project", "")

    cmd: list[str] = [
        "mngr",
        "create",
        ASSISTANT_AGENT_NAME,
        "--transfer",
        "none",
        "--template",
        "chat",
        "--no-connect",
        "--message",
        ASSISTANT_WELCOME_MESSAGE,
    ]
    if workspace_label:
        cmd.extend(["--label", f"workspace={workspace_label}"])
    if project_label:
        cmd.extend(["--label", f"project={project_label}"])

    logger.info("Creating assistant agent: {}", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        logger.warning(
            "Assistant create failed (rc={}); leaving sentinel absent so we retry next boot: {}",
            result.returncode,
            result.stderr.strip() or result.stdout.strip(),
        )
        return

    try:
        ASSISTANT_SENTINEL.parent.mkdir(parents=True, exist_ok=True)
        ASSISTANT_SENTINEL.write_text("")
    except OSError as e:
        logger.warning(
            "Could not write assistant sentinel {}: {}", ASSISTANT_SENTINEL, e
        )
        return
    logger.info("Assistant agent ready; sentinel written at {}", ASSISTANT_SENTINEL)


def main() -> None:
    session = _get_session_name()
    if not session:
        logger.error("Not running inside a tmux session")
        sys.exit(1)

    logger.info("Bootstrap service manager started (session: {})", session)

    _init_runtime_worktree()
    _ensure_assistant_agent()

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
