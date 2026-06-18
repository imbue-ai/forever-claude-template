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

from host_backup.config import (
    BACKUP_TOML_PATH,
    RESTIC_ENV_PATH,
    SnapshotMethod,
    SnapshotSettings,
    merge_snapshot_into_existing_toml,
    render_default_backup_toml,
    write_default_restic_env_template,
)
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
# Tmux window user-option that a service window sets to its command's exit
# status once the command returns. The manager polls this to detect a service
# that has exited -- the window itself stays open at an idle shell, so its mere
# existence is not a liveness signal -- and then applies the `restart` policy.
SVC_EXIT_STATUS_OPTION = "@svc_exit_status"
# Supported `restart` policies for a service in services.toml. "never" leaves an
# exited service dead; "on-failure" restarts it when it exits non-zero. An
# unrecognized value falls back to DEFAULT_RESTART_POLICY (with a warning).
DEFAULT_RESTART_POLICY = "never"
VALID_RESTART_POLICIES = frozenset({"never", "on-failure"})
POLL_INTERVAL = 5  # seconds

RUNTIME_DIR = Path("runtime")
RUNTIME_PREEXISTING_DIR = Path("runtime.preexisting")
RUNTIME_BACKUP_USER_NAME = "runtime-backup"
RUNTIME_BACKUP_USER_EMAIL = "runtime-backup@mindsbackup.local"

# Signal file gating exactly-once creation of the initial chat agent. Lives
# under runtime/ so the runtime-backup service replicates it to the
# mindsbackup/$MNGR_AGENT_ID branch (survives container loss).
INITIAL_CHAT_SIGNAL = RUNTIME_DIR / "initial_chat_created"
# Basename (under $MNGR_HOST_DIR) of the file holding the initial chat agent's id,
# read by system_interface's welcome_resend to address the resend by id.
INITIAL_CHAT_AGENT_ID_FILENAME = "initial_chat_agent_id"

# Env var names used by the bootstrap's new responsibilities.
_AGENT_ID_ENV_VAR = "MNGR_AGENT_ID"
_AGENT_STATE_DIR_ENV_VAR = "MNGR_AGENT_STATE_DIR"
_HOST_DIR_ENV_VAR = "MNGR_HOST_DIR"
_CLAUDE_CONFIG_DIR_ENV_VAR = "CLAUDE_CONFIG_DIR"


def _parse_env_file(content: str) -> dict[str, str]:
    """Parse a host env file (as produced by mngr's _format_env_file).

    Format spec mirrored from libs/mngr/.../hosts/host.py:_format_env_file:
    one `KEY=value` per line; values containing space, quote, or newline are
    double-quoted with `\\"` escaping. We accept blank lines and ignore them.

    Kept minimal (no shell-style expansion) so bootstrap doesn't need a
    python-dotenv dependency.
    """
    result: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1].replace('\\"', '"')
        result[key] = value
    return result


def _format_env_value(value: str) -> str:
    """Quote a value the same way mngr's _format_env_file does."""
    if any(c in value for c in (" ", '"', "'", "\n")):
        return '"' + value.replace('"', '\\"') + '"'
    return value


def _format_env_file(env: dict[str, str]) -> str:
    """Render an env dict back into the host env file format."""
    return "\n".join(f"{k}={_format_env_value(v)}" for k, v in env.items()) + "\n"


def _resolve_services_claude_config_dir() -> Path | None:
    """Return the services agent's per-agent Claude config dir.

    Mirrors mngr_claude's per-agent layout: $MNGR_AGENT_STATE_DIR/plugin/
    claude/anthropic. Returns None if the state-dir env var is not set,
    which only happens in tests or a broken container.
    """
    state_dir = os.environ.get(_AGENT_STATE_DIR_ENV_VAR, "")
    if not state_dir:
        logger.warning(
            "{} is unset; cannot resolve services agent Claude config dir",
            _AGENT_STATE_DIR_ENV_VAR,
        )
        return None
    return Path(state_dir) / "plugin" / "claude" / "anthropic"


def _ensure_host_claude_config_dir(target: Path) -> None:
    """Make sure $MNGR_HOST_DIR/env exports CLAUDE_CONFIG_DIR=<target>.

    Idempotent: only rewrites the env file when the key is missing or its
    current value differs from `target`. Future agents created on this host
    source this file at start-up (see build_source_env_shell_commands in
    mngr/.../hosts/host.py) and therefore inherit the right config dir
    without any per-agent intervention.
    """
    host_dir = os.environ.get(_HOST_DIR_ENV_VAR, "")
    if not host_dir:
        logger.warning(
            "{} is unset; skipping CLAUDE_CONFIG_DIR write to host env",
            _HOST_DIR_ENV_VAR,
        )
        return
    env_path = Path(host_dir) / "env"
    target_str = str(target)
    existing: dict[str, str] = {}
    if env_path.exists():
        try:
            existing = _parse_env_file(env_path.read_text())
        except OSError as e:
            logger.warning("Failed to read host env file at {}: {}", env_path, e)
            return
    if existing.get(_CLAUDE_CONFIG_DIR_ENV_VAR) == target_str:
        logger.debug(
            "Host env already has {}={}", _CLAUDE_CONFIG_DIR_ENV_VAR, target_str
        )
        return
    existing[_CLAUDE_CONFIG_DIR_ENV_VAR] = target_str
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(_format_env_file(existing))
    logger.info("Wrote {}={} to {}", _CLAUDE_CONFIG_DIR_ENV_VAR, target_str, env_path)


def _read_host_name() -> str | None:
    """Read host_name from $MNGR_HOST_DIR/data.json.

    Same source as system_interface._read_host_name. Returns None if any
    step fails so callers can decide whether to fall back.
    """
    host_dir = os.environ.get(_HOST_DIR_ENV_VAR, "")
    if not host_dir:
        return None
    data_path = Path(host_dir) / "data.json"
    if not data_path.exists():
        return None
    try:
        data = json.loads(data_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read {}: {}", data_path, e)
        return None
    name = data.get("host_name")
    if not isinstance(name, str) or not name:
        return None
    return name


def _read_main_agent_labels() -> dict[str, str]:
    """Read this agent's labels dict from $MNGR_HOST_DIR/agents/$MNGR_AGENT_ID/data.json.

    Returns an empty dict on any failure -- callers should treat missing
    labels as "skip --label flags rather than fail the create call".
    """
    host_dir = os.environ.get(_HOST_DIR_ENV_VAR, "")
    agent_id = os.environ.get(_AGENT_ID_ENV_VAR, "")
    if not host_dir or not agent_id:
        return {}
    data_path = Path(host_dir) / "agents" / agent_id / "data.json"
    if not data_path.exists():
        return {}
    try:
        data = json.loads(data_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read {}: {}", data_path, e)
        return {}
    labels = data.get("labels")
    if not isinstance(labels, dict):
        return {}
    # Pydantic-serialized dicts can carry non-string values; coerce defensively.
    return {str(k): str(v) for k, v in labels.items()}


def _resolve_initial_chat_workspace_label(labels: dict[str, str]) -> str | None:
    """Pick the workspace label value for the initial chat agent.

    Prefers the services agent's existing `workspace` label; falls back to
    `$MNGR_HOST_DIR/data.json`'s host_name so the chat agent still inherits
    a sensible workspace tag when the main-agent data.json is malformed.
    """
    workspace = labels.get("workspace")
    if workspace:
        return workspace
    return _read_host_name()


def _build_create_chat_command(host_name: str, labels: dict[str, str]) -> list[str]:
    """Build the `mngr create` argv for the initial chat agent.

    Mirrors the New Agent button's create path (see
    apps/system_interface/.../agent_manager.py:create_chat_agent): the
    `chat` template, no-connect, and inherited workspace/project labels
    when present on the services agent. Adds `--message /welcome`, which
    used to live on `create_templates.main`.
    """
    cmd: list[str] = [
        "mngr",
        "create",
        host_name,
        # `--transfer none` matches what `AgentManager.create_chat_agent`
        # uses for the "New Chat" button (apps/system_interface/.../
        # agent_manager.py). Without it, mngr defaults to creating a
        # per-agent git worktree on branch `mngr/<agent_name>` -- which
        # collides with the services agent's own worktree branch (set up
        # by the desktop client's `--branch :mngr/<host_name>` at host
        # create) and aborts with "fatal: a branch named 'mngr/<host>'
        # already exists". With --transfer none the chat agent reuses
        # the services agent's /mngr/code/ as its work_dir, which is what we
        # want (one workspace == one work_dir, shared across all chats).
        "--transfer",
        "none",
        "--template",
        "chat",
        "--message",
        "/welcome",
        "--no-connect",
        # JSON output so we can read back the created agent's id and persist it
        # for the welcome-resend target (see _persist_initial_chat_agent_id).
        "--format",
        "json",
    ]
    workspace = _resolve_initial_chat_workspace_label(labels)
    if workspace:
        cmd.extend(["--label", f"workspace={workspace}"])
    project = labels.get("project")
    if project:
        cmd.extend(["--label", f"project={project}"])
    return cmd


def _parse_created_agent_id(stdout: str) -> str | None:
    """Pull ``agent_id`` from `mngr create --format json` output, or None if absent."""
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and isinstance(data.get("agent_id"), str):
            return data["agent_id"]
    return None


def _persist_initial_chat_agent_id(agent_id: str) -> None:
    """Record the initial chat agent's id at `$MNGR_HOST_DIR/initial_chat_agent_id`.

    The welcome-resend target is read from here (system_interface's
    `welcome_resend`), so the resend addresses the agent by its stable id rather
    than re-resolving it by name.
    """
    host_dir = os.environ.get(_HOST_DIR_ENV_VAR, "")
    if not host_dir:
        logger.warning("{} unset; cannot persist initial chat agent id", _HOST_DIR_ENV_VAR)
        return
    (Path(host_dir) / INITIAL_CHAT_AGENT_ID_FILENAME).write_text(agent_id)
    logger.info("Persisted initial chat agent id {} for welcome resend", agent_id)


def _create_initial_chat_agent(host_name: str, labels: dict[str, str]) -> bool:
    """Invoke `mngr create` for the initial chat agent; persist its id. Returns success."""
    cmd = _build_create_chat_command(host_name, labels)
    logger.info("Creating initial chat agent: {}", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        logger.error(
            "Initial chat-agent create failed (rc={}): stdout={!r} stderr={!r}",
            result.returncode,
            result.stdout.strip(),
            result.stderr.strip(),
        )
        return False
    agent_id = _parse_created_agent_id(result.stdout)
    if agent_id is not None:
        _persist_initial_chat_agent_id(agent_id)
    else:
        logger.error("Initial chat agent created but could not parse agent_id from output: {!r}", result.stdout.strip())
    logger.info("Initial chat agent created")
    return True


def _touch_signal() -> None:
    """Write the runtime/initial_chat_created signal file."""
    INITIAL_CHAT_SIGNAL.parent.mkdir(parents=True, exist_ok=True)
    INITIAL_CHAT_SIGNAL.write_text("")


def _initialize_workspace_main_branch() -> None:
    """Commit any rsync-staged content and rename the work_dir branch to `main`.

    On first boot the work_dir (the services agent's $MNGR_AGENT_WORK_DIR,
    which the chat agent will share via `--transfer none`) is on whatever
    branch the desktop client's create flow assigned (typically
    `mngr/<host_name>` from agent_creator's `--branch :mngr/{host_name}`),
    with the desktop client's `_rsync_worktree_over_clone` content sitting
    as uncommitted changes on top of the shallow clone's tip.

    We want every new minds workspace to start out on a single clean
    `main` branch the user can git-log / push from without having to
    reason about the per-host mngr/* branch. So before the chat agent
    is created, we:
      1. set a minds-bootstrap committer identity if none is configured
      2. `git add -A` + `git commit` everything currently uncommitted
      3. `git branch -D main` (drop the stale shallow-clone main, if any)
      4. `git checkout -b main` (rename the working tree's branch to main)

    Each step is best-effort: a failure here should not prevent the
    chat-agent create from running. We log a warning and continue. Hooks
    are skipped with `--no-verify` because the user hasn't seen the
    workspace yet and a misbehaving pre-commit hook on the rsynced
    template shouldn't gate boot.
    """
    work_dir = os.environ.get("MNGR_AGENT_WORK_DIR", "")
    if not work_dir:
        logger.warning(
            "MNGR_AGENT_WORK_DIR is unset; skipping initial commit / main rename"
        )
        return

    def _git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=work_dir,
            capture_output=True,
            text=True,
            check=False,
        )

    # Set a committer identity scoped to this repo so the commit doesn't
    # fail on a container with no global git identity. We don't overwrite
    # an existing config -- only set if unset.
    if _git("config", "user.email").returncode != 0:
        _git("config", "user.email", "bootstrap@minds.local")
    if _git("config", "user.name").returncode != 0:
        _git("config", "user.name", "minds-bootstrap")

    _git("add", "-A")
    # --allow-empty so we end up with a commit even when the work_dir is
    # already clean (e.g. on second boot after a re-Create-from-snapshot,
    # though that path isn't wired up today). --no-verify skips any
    # pre-commit hooks the template repo may have configured.
    commit = _git(
        "commit", "--allow-empty", "--no-verify", "-m", "Initial workspace commit"
    )
    if commit.returncode != 0:
        logger.warning(
            "Initial workspace commit failed (rc={}): {}",
            commit.returncode,
            commit.stderr.strip() or commit.stdout.strip(),
        )

    # Drop any local `main` (the shallow clone's tip) so the rename
    # below has somewhere to land. `-D` is force-delete; harmless when
    # `main` doesn't exist.
    _git("branch", "-D", "main")
    # Rename / move the current branch to `main`. -M is force-rename
    # (move-over). On the very first boot the current branch is
    # `mngr/<host_name>`; on subsequent boots we may already be on `main`,
    # in which case `-M main` is a no-op.
    rename = _git("branch", "-M", "main")
    if rename.returncode != 0:
        logger.warning(
            "git branch -M main failed (rc={}): {}",
            rename.returncode,
            rename.stderr.strip() or rename.stdout.strip(),
        )
    else:
        logger.info("work_dir {} is now on branch main", work_dir)


def _maybe_create_initial_chat() -> None:
    """Create the initial chat agent on first boot, gated by a signal file.

    Also runs `_initialize_workspace_main_branch` immediately before the
    chat-agent create so the chat agent inherits a clean `main` branch.
    Both steps are gated by the same signal file, so they run exactly
    once per workspace.

    Touches the signal file only on a successful create -- a failed create
    leaves the signal file absent so the next bootstrap run retries. The
    user's manually-destroyed initial chat agent is *not* recreated,
    because the signal file persists in the runtime-backup branch.
    """
    if INITIAL_CHAT_SIGNAL.exists():
        logger.debug(
            "Signal file {} present; skipping initial chat create", INITIAL_CHAT_SIGNAL
        )
        return
    host_name = _read_host_name()
    if not host_name:
        logger.warning(
            "Could not resolve host_name; skipping initial chat agent create"
        )
        return
    _initialize_workspace_main_branch()
    labels = _read_main_agent_labels()
    if not _create_initial_chat_agent(host_name, labels):
        return
    _touch_signal()
    logger.info("Wrote signal file {}", INITIAL_CHAT_SIGNAL)


def _bootstrap_init_chat_dir() -> None:
    """Write CLAUDE_CONFIG_DIR to host env, then create initial chat if needed.

    Ordering matters: the env write must precede the chat-agent create so
    the new agent's claude binary sees CLAUDE_CONFIG_DIR via the host env
    file mngr sources at agent startup. Failures in either step are
    non-fatal so services still come up and the user has a working UI.
    """
    config_dir = _resolve_services_claude_config_dir()
    if config_dir is not None:
        _ensure_host_claude_config_dir(config_dir)
    _maybe_create_initial_chat()


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


def _get_window_exit_status(session: str, window_name: str) -> str:
    """Read a service window's recorded exit status.

    Returns the exit status string set by the recorder in
    ``_build_service_keystrokes``, or "" if the service is still running (the
    option stays unset until its command returns).
    """
    target = f"{session}:{window_name}"
    result = subprocess.run(
        ["tmux", "show-options", "-t", target, "-w", "-v", SVC_EXIT_STATUS_OPTION],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _list_exited_services(
    session: str, current: dict[str, dict[str, str]]
) -> dict[str, str]:
    """Return ``{service_name: exit_status}`` for managed windows that exited."""
    exited: dict[str, str] = {}
    for name, info in current.items():
        status = _get_window_exit_status(session, info["window_name"])
        if status != "":
            exited[name] = status
    return exited


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
            "restart": _normalize_restart_policy(name, config.get("restart")),
        }
    return result


def _normalize_restart_policy(name: str, restart: str | None) -> str:
    """Validate a service's `restart` value, defaulting unknown ones with a warning.

    An absent policy is the default and is not flagged. A present-but-unrecognized
    policy (e.g. a typo like "on_failure") is almost certainly a misconfiguration,
    so it is logged and falls back to DEFAULT_RESTART_POLICY rather than silently
    disabling restarts for that service.
    """
    if restart is None:
        return DEFAULT_RESTART_POLICY
    if restart not in VALID_RESTART_POLICIES:
        logger.warning(
            "Service {} has unrecognized restart policy {!r}; expected one of {}; "
            "treating as {!r}",
            name,
            restart,
            sorted(VALID_RESTART_POLICIES),
            DEFAULT_RESTART_POLICY,
        )
        return DEFAULT_RESTART_POLICY
    return restart


def _build_service_keystrokes(command: str, window_target: str) -> str:
    """Build the keystrokes typed into a service window's shell.

    Runs the service command, then records its exit status into the window's
    ``SVC_EXIT_STATUS_OPTION`` user option. The recorder runs in the same shell
    once the service returns, so the manager can poll that option to detect a
    service that has exited; the window itself stays open at an idle shell, so
    its existence alone is not a liveness signal.

    The ``set-option`` is given an explicit ``-t {window_target}``: a service
    window runs in the background, and a ``tmux set-option -w`` with no target
    applies to the session's *currently active* window, not the window the
    command runs in -- so without the explicit target the status would be
    written to the wrong window and the manager would never see the exit.

    ``$?`` is captured immediately after the command, so it reflects the
    service's own exit status (128+signal if the service process was killed
    while its shell survived).
    """
    return (
        f"{command}; tmux set-option -t {window_target} -w "
        f'{SVC_EXIT_STATUS_OPTION} "$?"'
    )


def _start_service(session: str, name: str, command: str) -> None:
    """Start a service in a new tmux window.

    Creates the window without a command so it uses the session's default-command
    (which sources env files), then sends the service command via send-keys.
    This ensures the service process inherits MNGR_AGENT_STATE_DIR and other
    agent environment variables. Records the command on the window via a user
    option so subsequent reconciles can detect command edits. The sent
    keystrokes also append an exit-status recorder (see
    ``_build_service_keystrokes``) so the manager can detect an exited service
    and apply its `restart` policy.
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
        [
            "tmux",
            "send-keys",
            "-t",
            window_target,
            _build_service_keystrokes(command, window_target),
            "Enter",
        ],
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


def _restart_service(session: str, name: str, command: str) -> None:
    """Restart an exited service: kill its stale window and start a fresh one.

    The fresh window has no ``SVC_EXIT_STATUS_OPTION`` set, so the manager
    does not immediately see it as exited again.
    """
    logger.info("Restarting exited service: {}", name)
    _stop_service(session, name)
    _start_service(session, name, command)


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


def _compute_restarts(
    desired: dict[str, dict],
    exited: dict[str, str],
) -> list[str]:
    """Return the names of exited services that should be restarted.

    Honors each service's `restart` policy from services.toml:
      - "never" (default): never restarted.
      - "on-failure": restarted only when the recorded exit status is
        non-zero (a clean exit is left alone).

    A service that exited but is no longer in `desired` (removed from
    services.toml) is skipped -- the mtime-driven reconcile removes its
    window instead.
    """
    restarts: list[str] = []
    for name, status in exited.items():
        config = desired.get(name)
        if config is None:
            continue
        if (
            config.get("restart", DEFAULT_RESTART_POLICY) == "on-failure"
            and status != "0"
        ):
            restarts.append(name)
    return restarts


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
        # A prior init may have staged runtime/ content aside and been killed
        # before restoring it (leaving runtime.preexisting/ behind while the
        # worktree itself already exists). Recover that content now rather
        # than stranding it. _restore_preexisting_into_worktree no-ops when
        # runtime.preexisting/ is absent, which is the common case.
        _restore_preexisting_into_worktree()
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

    # Restore staged-aside content. Calling unconditionally (rather than
    # gating on the `staged_aside` flag) also recovers content left by a
    # prior init that staged aside but was killed before it could restore.
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


def detect_snapshot_settings(
    *,
    trigger_dir: Path,
    host_dir: Path,
) -> SnapshotSettings:
    """Probe the container's filesystem to choose the right snapshot mechanism.

    Decision tree:
      - If `trigger_dir` exists as a directory, we are inside a vps-docker
        agent container with the snapshot-trigger volume mounted.
      - Else if `host_dir` is on a btrfs filesystem (lima), we can take
        snapshots directly via `sudo btrfs subvolume snapshot`.
      - Else (plain docker / any unrecognized provider) fall back to DIRECT
        with no snapshot.

    The returned settings carry the well-known paths each provider's
    cloud-init / lima provisioning makes available. Bootstrap is the
    single source of truth for these paths.
    """
    if trigger_dir.is_dir():
        # vps-docker: snapshots dir is bind-mounted at /mngr-snapshots; the
        # outer helper resolves <btrfs-mount>/<host_id_hex>/snapshots/current
        # at request time, so the inner script doesn't need to know the
        # outer-side path -- it just hands back what's at /mngr-snapshots/current.
        return SnapshotSettings(
            method=SnapshotMethod.OUTER_TRIGGER,
            btrfs_mount_path=Path("/mngr-btrfs"),
            host_subvolume_path=Path("/mngr-btrfs/<host_id_hex>"),
            snapshot_current_path=Path("/mngr-btrfs/snapshots/current"),
            snapshot_read_path=Path("/mngr-snapshots/current"),
            trigger_dir=trigger_dir,
        )
    fstype = _findmnt_fstype(host_dir)
    if fstype == "btrfs":
        # lima attaches a btrfs additional disk and symlinks host_dir to its
        # mount point, so the btrfs filesystem *is* host_dir. The snapshot must
        # live on that same btrfs (you cannot snapshot a subvolume onto another
        # filesystem), so derive every path from host_dir. The previous
        # hardcoded /mnt/host-volume only ever existed in the docker/vps layout,
        # which takes the OUTER_TRIGGER branch above -- never this one -- so on
        # lima it pointed snapshots at a plain dir on the root fs and the
        # `btrfs subvolume snapshot` failed with "not a btrfs filesystem".
        return SnapshotSettings(
            method=SnapshotMethod.BTRFS_LOCAL,
            btrfs_mount_path=host_dir,
            host_subvolume_path=host_dir,
            snapshot_current_path=host_dir / "snapshots" / "current",
            snapshot_read_path=host_dir / "snapshots" / "current",
        )
    return SnapshotSettings(
        method=SnapshotMethod.DIRECT,
        snapshot_read_path=host_dir,
    )


def _findmnt_fstype(path: Path) -> str:
    """Return the filesystem type for `path` via `findmnt`; empty string on any failure."""
    result = subprocess.run(
        ["findmnt", "-n", "-o", "FSTYPE", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def init_backup_config_with_settings(
    snapshot: SnapshotSettings,
    *,
    backup_toml_path: Path,
    restic_env_path: Path,
) -> None:
    """Write/merge backup.toml and write default restic.env (if absent).

    Pure-ish: takes its inputs by argument so tests can target it directly
    without monkeypatching environment detection or filesystem paths. Best-
    effort: logs and returns rather than raising on any failure.
    """
    logger.info(
        "host_backup snapshot method: {} (subvolume={}, trigger_dir={})",
        snapshot.method.value,
        snapshot.host_subvolume_path,
        snapshot.trigger_dir,
    )
    backup_toml_path.parent.mkdir(parents=True, exist_ok=True)
    if backup_toml_path.exists():
        try:
            existing = backup_toml_path.read_text()
            merged = merge_snapshot_into_existing_toml(existing, snapshot)
        except (OSError, ValueError) as e:
            logger.warning(
                "Failed to merge new snapshot section into {}: {}", backup_toml_path, e
            )
            return
        if merged != existing:
            backup_toml_path.write_text(merged)
            logger.info("Updated [snapshot] section of {}", backup_toml_path)
    else:
        try:
            backup_toml_path.write_text(render_default_backup_toml(snapshot))
        except OSError as e:
            logger.warning("Failed to write default {}: {}", backup_toml_path, e)
            return
        logger.info("Wrote default {}", backup_toml_path)
    try:
        written = write_default_restic_env_template(restic_env_path)
    except OSError as e:
        logger.warning("Failed to write restic.env template: {}", e)
        return
    if written:
        logger.info(
            "Wrote default restic.env template (must be populated before backups run)"
        )


def _init_backup_config() -> None:
    """Top-level orchestrator: detect env, then delegate to init_backup_config_with_settings."""
    try:
        snapshot = detect_snapshot_settings(
            trigger_dir=Path("/mngr-snapshot"),
            host_dir=Path("/mngr"),
        )
    except OSError as e:
        logger.warning("Failed to detect snapshot environment: {}", e)
        return
    init_backup_config_with_settings(
        snapshot,
        backup_toml_path=BACKUP_TOML_PATH,
        restic_env_path=RESTIC_ENV_PATH,
    )


def main() -> None:
    session = _get_session_name()
    if not session:
        logger.error("Not running inside a tmux session")
        sys.exit(1)

    logger.info("Bootstrap service manager started (session: {})", session)

    # Restore runtime/ FIRST so the initial_chat_created signal file (which
    # lives inside the worktree and is replicated to mindsbackup/$MNGR_AGENT_ID
    # by the runtime-backup service) is in place before we decide whether to
    # create the initial chat agent. Without this ordering, every container
    # restart sees an empty runtime/, treats the boot as first-ever, and
    # re-creates the welcome chat agent (and auto-commits any uncommitted
    # work_dir state).
    _init_runtime_worktree()

    _bootstrap_init_chat_dir()

    # Detect the snapshot environment and write runtime/backup.toml +
    # runtime/secrets/restic.env so the svc-host-backup window can come up
    # with a coherent default config. Re-runs on every boot to keep
    # snapshot.method in sync with the detected provider; user-customized
    # fields in backup.toml are preserved via toml-merge.
    _init_backup_config()

    last_mtime = None
    # Cache of the parsed services.toml, refreshed only when the file's mtime
    # changes. Parsing (and the per-service restart-policy validation/warnings
    # in _load_services) must NOT run every poll: the restart loop below needs
    # `desired` each iteration, but re-reading the file every POLL_INTERVAL
    # would re-emit any unrecognized-policy warning on every tick and re-parse
    # the file needlessly. So we load once per change and reuse the cache.
    desired: dict[str, dict] = {}

    while True:
        current_mtime = _get_file_mtime()

        # Reconcile (and reload the cached services) on startup or when
        # services.toml changes.
        if current_mtime != last_mtime:
            desired = _load_services()
            current = _list_managed_windows(session)
            _reconcile(session, desired, current)
            last_mtime = current_mtime

        # Independently of services.toml edits, detect services whose process
        # has exited and apply each service's `restart` policy. The reconcile
        # above only fires on mtime changes, and a crashed service leaves its
        # tmux window open at an idle shell -- so without this a crashed
        # service would stay dead, and the `restart` policy would be inert.
        current = _list_managed_windows(session)
        exited = _list_exited_services(session, current)
        for name in _compute_restarts(desired, exited):
            _restart_service(session, name, desired[name]["command"])

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
