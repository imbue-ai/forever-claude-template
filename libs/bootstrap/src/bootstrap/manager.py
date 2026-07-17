"""Bootstrap: first-boot setup, then launch supervisord.

`uv run bootstrap` runs once per container boot (from the `bootstrap`
extra_window). It performs first-boot setup -- global git config, writing
CLAUDE_CONFIG_DIR into the host env, and creating the initial chat agent --
and then `exec`s the system supervisord in the foreground. supervisord
(configured by supervisord.conf) owns every background service from then on.

Running supervisord via exec keeps the bootstrap tmux window alive as
supervisord and lets the supervised services inherit this shell's already-
sourced agent environment (MNGR_AGENT_STATE_DIR, CLAUDE_CONFIG_DIR, etc.).
"""

import json
import os
import re
import shlex
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from loguru import logger

# Path (relative to the repo root, which is bootstrap's cwd) of the supervisord
# config that defines every background service.
SUPERVISORD_CONF = Path("supervisord.conf")
# Container-local directory for supervisord's own log + the per-service logs. Not
# under runtime/, so these are never backed up.
SUPERVISOR_LOG_DIR = Path("/var/log/supervisor")

RUNTIME_DIR = Path("runtime")

# The Caretaker's state dir (run logs + permissions.md, written by the
# caretaker skill) lives under runtime/ so it rides the runtime-backup branch.
CARETAKER_DIR = RUNTIME_DIR / "caretaker"

# Env snapshot for cron jobs. cron builds a minimal environment for its
# jobs, so none of the agent env (MNGR_*, LATCHKEY_*, GH_TOKEN, the PATH with
# /root/.local/bin, ...) survives into them. Bootstrap has the full agent env
# (supervisord and every service inherit it from this shell), so it dumps a
# snapshot here each boot for scripts/with_agent_env.sh to source. /run is a
# tmpfs, so the snapshot never outlives the boot that wrote it.
AGENT_ENV_SNAPSHOT_PATH = Path("/run/minds-agent-env")
# Keys must be valid shell identifiers to be re-exported (cron jobs source the
# snapshot through bash); anything else (e.g. bash exports function baggage) is
# skipped.
_ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Timezone-at-boot: the minds desktop client serves the user's IANA timezone at
# GET /api/v1/timezone; bootstrap reaches it through the latchkey gateway's
# minds-api-proxy and points /etc/localtime + /etc/timezone at it, so cron
# schedules run in the user's local time. The gateway's reverse tunnel may not
# be up yet this early in boot, hence the small bounded retry.
_TIMEZONE_FETCH_ATTEMPTS = 3
_TIMEZONE_FETCH_RETRY_SECONDS = 3.0
_TIMEZONE_FETCH_TIMEOUT_SECONDS = 5.0

# Signal file gating exactly-once creation of the initial chat agent. Lives
# under runtime/, which persists with the container volume (and is synced to
# GitHub when the opt-in github-sync skill has been enabled).
INITIAL_CHAT_SIGNAL = RUNTIME_DIR / "initial_chat_created"
# Basename (under $MNGR_HOST_DIR) of the file holding the initial chat agent's id,
# read by system_interface's welcome_resend to address the resend by id.
INITIAL_CHAT_AGENT_ID_FILENAME = "initial_chat_agent_id"

# Env var names used by the bootstrap's new responsibilities.
_AGENT_ID_ENV_VAR = "MNGR_AGENT_ID"
_AGENT_STATE_DIR_ENV_VAR = "MNGR_AGENT_STATE_DIR"
_HOST_DIR_ENV_VAR = "MNGR_HOST_DIR"
_CLAUDE_CONFIG_DIR_ENV_VAR = "CLAUDE_CONFIG_DIR"

# Global git config applied on every boot: rewrite git@ / ssh:// GitHub
# remotes to https (there are no SSH credentials in the container). Note that
# git applies at most one insteadOf rewrite per URL, so this rewrite's output
# is NOT further rewritten by github-sync's latchkey gateway wiring: only
# remotes stored as https://github.com/ URLs (the shape the github-sync skill
# always configures) route through the gateway.
# core.hooksPath is deliberately NOT set here -- the post-commit auto-push
# hook only becomes active when the github-sync skill wires it up.
_GIT_GLOBAL_CONFIG_ARGVS = (
    (
        "config",
        "--global",
        "--replace-all",
        "url.https://github.com/.insteadOf",
        "git@github.com:",
    ),
    (
        "config",
        "--global",
        "--add",
        "url.https://github.com/.insteadOf",
        "ssh://git@github.com/",
    ),
)


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


def _build_create_chat_command(host_name: str, labels: dict[str, str]) -> list[str]:
    """Build the `mngr create` argv for the initial chat agent.

    Mirrors the New Agent button's create path (see
    apps/system_interface/.../agent_manager.py:create_chat_agent): the
    `chat` template, no-connect, and the inherited `project` label when
    present on the services agent. Adds `--message /welcome`, which used to
    live on `create_templates.main`. The chat agent belongs to its workspace
    by virtue of sharing the host; it carries no `workspace` label.
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
        # Tags the initial chat as a user-created agent so the OOM agent-tagging
        # hook puts it in the protected user-agent band (matching the New Chat /
        # New Agent paths in apps/system_interface).
        "--label",
        "user_created=true",
        "--no-connect",
        "--format",
        "json",
    ]
    project = labels.get("project")
    if project:
        cmd.extend(["--label", f"project={project}"])
    return cmd


def _parse_created_agent_id(stdout: str) -> str | None:
    """Pull ``agent_id`` from `mngr create --format json` stdout, or None if absent.

    `--format json` writes a single JSON object to stdout (logs go to stderr).
    None on any malformed/missing case keeps the caller non-fatal.
    """
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict) and isinstance(data.get("agent_id"), str):
        return data["agent_id"]
    return None


def _persist_initial_chat_agent_id(agent_id: str) -> None:
    """Record the initial chat agent's id at `$MNGR_HOST_DIR/initial_chat_agent_id`.

    The welcome-resend target is read from here (system_interface's
    `welcome_resend`), so the resend addresses the agent by its stable id rather
    than re-resolving it by name. Best-effort: a missing host dir or a failed
    write is logged but not raised, so it never aborts the create/signal flow
    (the welcome-resend simply skips when the file is absent).
    """
    host_dir = os.environ.get(_HOST_DIR_ENV_VAR, "")
    if not host_dir:
        logger.warning(
            "{} unset; cannot persist initial chat agent id", _HOST_DIR_ENV_VAR
        )
        return
    try:
        (Path(host_dir) / INITIAL_CHAT_AGENT_ID_FILENAME).write_text(agent_id)
    except OSError as e:
        logger.error("Failed to persist initial chat agent id {}: {}", agent_id, e)
        return
    logger.info("Persisted initial chat agent id {} for welcome resend", agent_id)


def _find_existing_chat_agent_id(host_name: str) -> str | None:
    """Return the id of an existing agent named after the host, if exactly one exists.

    The initial chat agent is named after its host, so on a clean first boot
    no such agent exists and this returns None. But a previous boot's create
    can fail *after* registering the agent -- e.g. the `--message /welcome`
    delivery timed out because claude sat at its no-credentials login screen
    and never signalled ready. The agent survives with no recorded id, and
    re-running `mngr create` would fail with a name collision on every
    subsequent boot, so the retry must adopt the survivor instead. Mirrors
    the lookup-first shape of scripts/run_task_agent.sh, including --active,
    so an archived leftover (or one on a dead host) is never adopted as the
    welcome-resend target. Returns None on lookup failure or ambiguity,
    keeping the caller on the plain create path.
    """
    result = subprocess.run(
        [
            "mngr",
            "list",
            "--active",
            "--include",
            f'name == "{host_name}"',
            "--ids",
            "--on-error",
            "continue",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        logger.warning(
            "Existing chat-agent lookup failed (rc={}): {}",
            result.returncode,
            result.stderr.strip(),
        )
        return None
    agent_ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(agent_ids) > 1:
        logger.warning(
            "Multiple agents named {}; skipping adopt: {}", host_name, agent_ids
        )
        return None
    if not agent_ids:
        return None
    return agent_ids[0]


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
        logger.error(
            "Initial chat agent created but could not parse agent_id from output: {!r}",
            result.stdout.strip(),
        )
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
    because the signal file persists in runtime/.
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
    existing_agent_id = _find_existing_chat_agent_id(host_name)
    if existing_agent_id is not None:
        # An earlier boot's create registered the agent but died before
        # recording it (see _find_existing_chat_agent_id). Adopt it: persist
        # its id -- the welcome-resend target -- and write the signal so we
        # stop re-creating. The welcome itself arrives through the
        # auth-success resend chokepoint once the user signs in
        # (system_interface/welcome_resend.py); re-sending `/welcome` from
        # here would race claude's login screen all over again.
        logger.info(
            "Adopting chat agent {} left by an earlier partial create",
            existing_agent_id,
        )
        _persist_initial_chat_agent_id(existing_agent_id)
        _touch_signal()
        logger.info("Wrote signal file {}", INITIAL_CHAT_SIGNAL)
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


def _configure_git_global() -> None:
    """Apply the boot-time global git config.

    Rewrites git@ / ssh:// GitHub remotes to https (see
    _GIT_GLOBAL_CONFIG_ARGVS). Best-effort: a failure here should not block
    the supervisord launch.
    """
    for argv in _GIT_GLOBAL_CONFIG_ARGVS:
        result = subprocess.run(
            ["git", *argv], capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            logger.warning(
                "git {} failed (rc={}): {}",
                " ".join(argv),
                result.returncode,
                result.stderr.strip(),
            )


def _write_agent_env_snapshot(path: Path = AGENT_ENV_SNAPSHOT_PATH) -> None:
    """Dump this process's environment as `export KEY=<value>` lines at `path`.

    scripts/with_agent_env.sh sources the file to rebuild the agent environment
    inside cron jobs (see AGENT_ENV_SNAPSHOT_PATH). Values are
    shell-quoted; keys that are not valid shell identifiers are skipped. Written
    0600 because the env carries secrets (GH_TOKEN, gateway password).
    Best-effort: logs and returns rather than raising.
    """
    lines = []
    for key, value in sorted(os.environ.items()):
        if not _ENV_KEY_PATTERN.fullmatch(key):
            continue
        lines.append(f"export {key}={shlex.quote(value)}")
    content = "\n".join(lines) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
        # os.open's mode only applies at creation; tighten a pre-existing file.
        path.chmod(0o600)
    except OSError as e:
        logger.warning("Failed to write agent env snapshot to {}: {}", path, e)
        return
    logger.info("Wrote agent env snapshot to {}", path)


def _fetch_user_timezone() -> str:
    """Fetch the user's IANA timezone name from the minds desktop client.

    GETs /api/v1/timezone through the latchkey gateway's minds-api-proxy using
    the gateway env vars mngr injects into the agent environment. The gateway's
    reverse tunnel may not be up yet this early in boot, so the fetch retries a
    few times before giving up. Returns "" on any failure (missing env, refused
    connection, non-200, malformed body) so the caller can fall back to UTC.
    """
    gateway = os.environ.get("LATCHKEY_GATEWAY", "")
    password = os.environ.get("LATCHKEY_GATEWAY_PASSWORD", "")
    permissions = os.environ.get("LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE", "")
    if not gateway or not password or not permissions:
        logger.debug("Latchkey gateway env not fully set; skipping timezone fetch")
        return ""
    request = urllib.request.Request(
        f"{gateway.rstrip('/')}/minds-api-proxy/api/v1/timezone",
        headers={
            "X-Latchkey-Gateway-Password": password,
            "X-Latchkey-Gateway-Permissions-Override": permissions,
        },
    )
    last_error: Exception | None = None
    for attempt in range(_TIMEZONE_FETCH_ATTEMPTS):
        if attempt > 0:
            # The bounded sleep between attempts is deliberate (and carried by
            # the time_sleep ratchet): there is no readiness event for the
            # gateway's reverse tunnel to wait on this early in boot.
            time.sleep(_TIMEZONE_FETCH_RETRY_SECONDS)
        try:
            with urllib.request.urlopen(
                request, timeout=_TIMEZONE_FETCH_TIMEOUT_SECONDS
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError) as e:
            # OSError covers URLError/HTTPError (refused, 403/503, timeout);
            # ValueError covers a non-JSON or non-UTF-8 body.
            last_error = e
            continue
        timezone_name = payload.get("timezone") if isinstance(payload, dict) else None
        if isinstance(timezone_name, str) and timezone_name:
            return timezone_name
        last_error = ValueError(f"unexpected timezone payload: {payload!r}")
    logger.warning(
        "Could not fetch the user timezone from the gateway after {} attempts "
        "({}); container stays on UTC",
        _TIMEZONE_FETCH_ATTEMPTS,
        last_error,
    )
    return ""


def _apply_container_timezone(
    tz_name: str,
    zoneinfo_dir: Path = Path("/usr/share/zoneinfo"),
    localtime_path: Path = Path("/etc/localtime"),
    timezone_path: Path = Path("/etc/timezone"),
) -> bool:
    """Point /etc/localtime and /etc/timezone at the named IANA zone.

    The name is validated by loading it with ``ZoneInfo`` -- the same check the
    minds desktop client applies before serving the value -- which by spec
    rejects absolute paths and ``..`` components (so a malicious response
    cannot traverse out of the zoneinfo dir) and proves the zone is real. The
    ``is_file`` check below still matters: ZoneInfo may resolve a zone from
    elsewhere on TZPATH, but the symlink must point into ``zoneinfo_dir``
    specifically. The localtime swap is a temp symlink + os.replace so a
    concurrent reader never sees the file missing. Must run before supervisord
    starts cron: cron reads the timezone once at daemon start. Best-effort:
    returns False with a warning on any failure.
    """
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError, OSError):
        logger.warning("Ignoring invalid timezone name {!r}", tz_name)
        return False
    zone_file = zoneinfo_dir / tz_name
    if not zone_file.is_file():
        logger.warning("Timezone {!r} has no zoneinfo file at {}", tz_name, zone_file)
        return False
    try:
        tmp_link = localtime_path.with_name(localtime_path.name + ".minds-tmp")
        tmp_link.unlink(missing_ok=True)
        tmp_link.symlink_to(zone_file)
        os.replace(tmp_link, localtime_path)
        timezone_path.write_text(tz_name + "\n")
    except OSError as e:
        logger.warning("Failed to apply timezone {!r}: {}", tz_name, e)
        return False
    logger.info("Container timezone set to {}", tz_name)
    return True


def _ensure_supervisor_log_dir() -> None:
    """Create supervisord's log directory if missing.

    supervisord and its child programs write into SUPERVISOR_LOG_DIR but do not
    create it, so it must exist before we exec supervisord. Best-effort.
    """
    try:
        SUPERVISOR_LOG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning(
            "Failed to create supervisor log dir {}: {}", SUPERVISOR_LOG_DIR, e
        )


def _exec_supervisord() -> None:
    """Replace this process with supervisord running in the foreground.

    Uses the system supervisord (installed via scripts/setup_system.sh) and the
    repo-root supervisord.conf. `-n` keeps it in the foreground (so the
    bootstrap tmux window stays alive as supervisord) while still creating the
    [unix_http_server] socket that `supervisorctl` talks to.
    """
    logger.info("Launching supervisord with config {}", SUPERVISORD_CONF)
    os.execvp("supervisord", ["supervisord", "-n", "-c", str(SUPERVISORD_CONF)])


def _ensure_caretaker_dir() -> None:
    """Create runtime/caretaker/ (the Caretaker's run logs + permissions.md).

    Best-effort: logs and returns rather than raising so a transient FS error
    here does not block the supervisord launch.
    """
    try:
        CARETAKER_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("Failed to create caretaker runtime dir: {}", e)


def main() -> None:
    logger.info("Bootstrap starting: first-boot setup, then supervisord")

    # Apply the global git config (https rewrites) before any service or
    # agent runs git.
    _configure_git_global()

    # Snapshot the agent environment for cron jobs (they get a scrubbed
    # env; scripts/with_agent_env.sh sources this snapshot), and set the
    # container clock to the user's timezone so schedules run in their local
    # time. All of this must precede _exec_supervisord: cron reads the timezone
    # once at daemon start, and the Caretaker's daily-job stamp must be seeded
    # before cron's first tick of scripts/run_daily_job.sh or a workspace
    # created after 3 AM spawns the Caretaker immediately on creation.
    _write_agent_env_snapshot()
    tz_name = _fetch_user_timezone()
    if tz_name:
        _apply_container_timezone(tz_name)

    _bootstrap_init_chat_dir()

    # Create runtime/caretaker/ (the Caretaker's state dir) after the runtime
    # worktree is in place, so it rides the backup branch. The Caretaker itself
    # runs as a daily job via scripts/run_daily_job.sh (see the
    # /etc/cron.d/minds-caretaker line written by scripts/build_workspace.sh).
    _ensure_caretaker_dir()

    # Make sure supervisord's log directory exists, then hand off: replace this
    # process with supervisord in the foreground. supervisord owns every
    # background service from here on (see supervisord.conf).
    _ensure_supervisor_log_dir()
    _exec_supervisord()


if __name__ == "__main__":
    main()
