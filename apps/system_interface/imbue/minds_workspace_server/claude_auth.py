"""In-mind Claude authentication recovery: status checks, OAuth PTY flow, API-key write.

Implements the backend half of the in-UI Claude login modal so that a user
whose Claude credentials didn't sync into the mind can recover without
dropping into the ttyd terminal.

Two sign-in paths:

1. Subscription OAuth (`claude auth login --claudeai`) and Console OAuth
   (`claude auth login --console`) are driven via pexpect: the CLI prints
   an `oauth/authorize` URL (`https://claude.com/cai/oauth/authorize?...`
   for `--claudeai` and `https://platform.claude.com/oauth/authorize?...`
   for `--console`) and waits for a `CODE#STATE` paste on stdin. The PTY
   subprocess is held in module state between the `start_oauth_login` and
   `submit_oauth_code` calls so the UI can collect the code from the user
   in between. The completed flow writes the shared
   `$CLAUDE_CONFIG_DIR/.credentials.json` file, which every running claude
   in the mind auto-detects on its next API call -- no restart required.
2. Raw API key: `submit_api_key` writes `ANTHROPIC_API_KEY` into the host
   env file the bootstrap already manages and then restarts every
   `type: claude` agent in the mind via `mngr stop`/`mngr start`. The
   restart is necessary because env vars are inherited at process start
   and cannot be updated in-place; without it, the new key has no effect
   on already-running claudes.

Dependencies that touch the outside world (subprocess invocation and
pexpect-driven PTY spawning) are exposed as named callables on the module
so tests can substitute deterministic fakes without `unittest.mock`.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid as _uuid
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import Any
from typing import Final

import pexpect
from loguru import logger as _loguru_logger
from pydantic import Field
from pydantic import SecretStr

from imbue.concurrency_group.subprocess_utils import ProcessSetupError
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.utils.env_utils import parse_env_file

logger = _loguru_logger

_HOST_DIR_ENV_VAR = "MNGR_HOST_DIR"
_ANTHROPIC_API_KEY_ENV_VAR = "ANTHROPIC_API_KEY"
_OAUTH_URL_REGEX = re.compile(r"https://\S*oauth/authorize\S*")
_OAUTH_URL_WAIT_SECONDS: Final = 30.0
_OAUTH_COMPLETE_WAIT_SECONDS: Final = 30.0
_MNGR_COMMAND_TIMEOUT_SECONDS: Final = 60.0
_CLAUDE_AUTH_STATUS_TIMEOUT_SECONDS: Final = 10.0


class ClaudeAuthError(RuntimeError):
    """Raised when an auth flow operation cannot complete."""


# Public type aliases for dependency injection. Tests pass deterministic
# fakes; production code uses the module defaults.
CommandRunner = Callable[..., Any]
PexpectSpawner = Callable[..., Any]


def _default_command_runner(command: list[str], timeout: float) -> Any:
    return run_local_command_modern_version(
        command=command, is_checked=False, timeout=timeout, cwd=None
    )


def _default_pexpect_spawner(executable: str, args: list[str], timeout: float) -> Any:
    return pexpect.spawn(executable, args, timeout=timeout, encoding="utf-8")


# Module-level injectable dependencies. Production callers use the
# defaults; tests rebind these (claude_auth.command_runner = fake_runner)
# rather than passing fakes through layered call sites or using
# `unittest.mock`. Looking the values up at call time is intentional so
# that rebinding after import takes effect immediately.
command_runner: CommandRunner = _default_command_runner
pexpect_spawner: PexpectSpawner = _default_pexpect_spawner


class AuthStatus(FrozenModel):
    """Parsed output of `claude auth status --json`.

    `subscription_type` is unset for Console accounts (API-usage billing),
    so the frontend conditionally renders the success-state copy.
    """

    logged_in: bool = Field(description="Whether claude is currently authenticated")
    auth_method: str | None = Field(default=None, description="e.g. 'oauth', 'api_key'")
    api_provider: str | None = Field(default=None, description="e.g. 'anthropic', 'claudeai'")
    email: str | None = Field(default=None)
    org_id: str | None = Field(default=None)
    org_name: str | None = Field(default=None)
    subscription_type: str | None = Field(
        default=None, description="e.g. 'Max'; absent for Console accounts"
    )


class OAuthProvider(str, Enum):
    CLAUDEAI = "claudeai"
    CONSOLE = "console"


class OAuthStartResult(FrozenModel):
    session_id: str = Field(description="Opaque token for the in-flight OAuth session")
    oauth_url: str = Field(description="URL the user opens to authorize the login")


class _OAuthSessionRecord(FrozenModel):
    """Immutable handle for an in-flight OAuth subprocess.

    Pairs with a parallel non-frozen slot that holds the live pexpect
    process object, since that object is not Pydantic-serializable.
    """

    session_id: str
    provider: OAuthProvider
    oauth_url: str


def _coerce_str_or_none(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    return str(value)


def _parse_status_payload(payload: dict[str, object]) -> AuthStatus:
    return AuthStatus(
        logged_in=bool(payload.get("loggedIn", False)),
        auth_method=_coerce_str_or_none(payload.get("authMethod")),
        api_provider=_coerce_str_or_none(payload.get("apiProvider")),
        email=_coerce_str_or_none(payload.get("email")),
        org_id=_coerce_str_or_none(payload.get("orgId")),
        org_name=_coerce_str_or_none(payload.get("orgName")),
        subscription_type=_coerce_str_or_none(payload.get("subscriptionType")),
    )


def get_auth_status() -> AuthStatus:
    """Invoke `claude auth status --json` and parse the result.

    Returns `logged_in=False` if the `claude` binary is missing or doesn't
    produce output, rather than raising, since the whole point of the
    modal is to recover from broken auth state.
    """
    try:
        result = command_runner(
            ["claude", "auth", "status", "--json"], _CLAUDE_AUTH_STATUS_TIMEOUT_SECONDS
        )
    except ProcessSetupError as e:
        logger.warning("claude auth status failed to launch: {}", e)
        return AuthStatus(logged_in=False)

    stdout = result.stdout.strip() if isinstance(result.stdout, str) else ""
    if not stdout:
        return AuthStatus(logged_in=False)
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise ClaudeAuthError(f"claude auth status returned non-JSON output: {stdout!r}") from e
    if not isinstance(payload, dict):
        raise ClaudeAuthError(f"claude auth status returned non-object JSON: {payload!r}")
    return _parse_status_payload(payload)


def _format_env_file(env: dict[str, str]) -> str:
    """Render an env dict back into the host env file format (matches mngr's _format_env_file)."""
    lines: list[str] = []
    for key, value in env.items():
        if " " in value or '"' in value or "'" in value or "\n" in value:
            value = '"' + value.replace('"', '\\"') + '"'
        lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def _resolve_host_env_path() -> Path:
    host_dir = os.environ.get(_HOST_DIR_ENV_VAR, "")
    if not host_dir:
        raise ClaudeAuthError(f"{_HOST_DIR_ENV_VAR} is unset; cannot locate host env file")
    return Path(host_dir) / "env"


def write_api_key_to_host_env(api_key: SecretStr, env_path_override: Path | None = None) -> Path:
    """Persist `ANTHROPIC_API_KEY=<value>` into the host env file (idempotent).

    Mirrors the host-env-write pattern used by the bootstrap for
    `CLAUDE_CONFIG_DIR`. The host env is sourced when an agent's tmux
    session starts, so a `mngr stop`/`mngr start` of the chat agent
    afterwards picks the new key up.
    """
    env_path = env_path_override or _resolve_host_env_path()
    existing: dict[str, str] = {}
    if env_path.exists():
        existing = parse_env_file(env_path.read_text())
    existing[_ANTHROPIC_API_KEY_ENV_VAR] = api_key.get_secret_value()
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(_format_env_file(existing))
    return env_path


def list_claude_agent_names() -> list[str]:
    """Return the names of every `type: claude` agent in the local mind.

    Uses `mngr list --format json` and filters to `type == "claude"`. This
    excludes the `main`-type system-services agent, which has no
    interactive claude process to restart.
    """
    result = command_runner(
        ["mngr", "list", "--format", "json"], _MNGR_COMMAND_TIMEOUT_SECONDS
    )
    if result.returncode != 0:
        raise ClaudeAuthError(
            f"mngr list failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    stdout = result.stdout if isinstance(result.stdout, str) else ""
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise ClaudeAuthError(f"mngr list returned non-JSON output: {stdout!r}") from e
    if not isinstance(payload, dict):
        raise ClaudeAuthError(f"mngr list returned non-object JSON: {payload!r}")
    agents = payload.get("agents", [])
    if not isinstance(agents, list):
        raise ClaudeAuthError(f"mngr list 'agents' field is not a list: {agents!r}")
    names: list[str] = []
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        if agent.get("type") != "claude":
            continue
        name = agent.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return names


def restart_all_claude_agents() -> list[str]:
    """Restart every `type: claude` agent via `mngr stop` then `mngr start`.

    Returns the list of agent names that were restarted. Used by the
    API-key auth path so the freshly-written `ANTHROPIC_API_KEY` is in
    effect across every running claude in the mind, not just the one the
    user happened to be chatting with.
    """
    names = list_claude_agent_names()
    for name in names:
        logger.info("Restarting type:claude agent {} via mngr stop+start", name)
        stop_result = command_runner(["mngr", "stop", name], _MNGR_COMMAND_TIMEOUT_SECONDS)
        if stop_result.returncode != 0:
            raise ClaudeAuthError(
                f"mngr stop {name} failed (exit {stop_result.returncode}): {stop_result.stderr.strip()}"
            )
        start_result = command_runner(["mngr", "start", name], _MNGR_COMMAND_TIMEOUT_SECONDS)
        if start_result.returncode != 0:
            raise ClaudeAuthError(
                f"mngr start {name} failed (exit {start_result.returncode}): {start_result.stderr.strip()}"
            )
    return names


def submit_api_key(api_key: SecretStr) -> AuthStatus:
    """Write `ANTHROPIC_API_KEY` to host env then restart every claude agent.

    All `type: claude` agents must be restarted: env vars are read at
    process start, so already-running claudes won't pick up the new key
    until their tmux sessions are torn down and respawned.
    """
    write_api_key_to_host_env(api_key)
    restart_all_claude_agents()
    return get_auth_status()


# ---- OAuth PTY flow ----


_oauth_lock = threading.Lock()
_current_oauth_record: _OAuthSessionRecord | None = None
_current_oauth_process: Any = None


def _safe_terminate(process: Any) -> None:
    if not process.isalive():
        return
    try:
        process.terminate(force=True)
    except OSError as e:
        logger.warning("OAuth subprocess terminate raised: {}", e)


def _safe_close(process: Any) -> None:
    """Release the pexpect spawn's PTY file descriptor.

    `pexpect.spawn.close()` can raise `OSError` (e.g. on an already-closed
    descriptor) and `pexpect.ExceptionPexpect` in some teardown paths.
    Swallow + log both since the only thing we can do at this point is
    drop the reference anyway.
    """
    try:
        process.close()
    except (OSError, pexpect.ExceptionPexpect) as e:
        logger.warning("OAuth subprocess close raised: {}", e)


def _spawn_oauth_and_parse_url(provider: OAuthProvider) -> tuple[Any, str]:
    process = pexpect_spawner(
        "claude",
        ["auth", "login", f"--{provider.value}"],
        _OAUTH_URL_WAIT_SECONDS,
    )
    match_index = process.expect([_OAUTH_URL_REGEX, pexpect.EOF, pexpect.TIMEOUT])
    if match_index != 0:
        _safe_terminate(process)
        _safe_close(process)
        if match_index == 1:
            raise ClaudeAuthError("claude auth login exited before printing OAuth URL")
        raise ClaudeAuthError("Timed out waiting for OAuth URL from claude auth login")
    match = process.match
    if match is None:
        _safe_terminate(process)
        _safe_close(process)
        raise ClaudeAuthError("OAuth URL regex matched but pexpect.match is None (unexpected)")
    return process, match.group(0)


def start_oauth_login(provider: OAuthProvider) -> OAuthStartResult:
    """Spawn `claude auth login --<provider>` and return the parsed OAuth URL.

    Replaces any prior in-flight session: only one OAuth flow can be live
    at a time per process, which matches the single-mind / single-user
    deployment model.
    """
    global _current_oauth_record, _current_oauth_process
    with _oauth_lock:
        if _current_oauth_process is not None:
            _safe_terminate(_current_oauth_process)
            _safe_close(_current_oauth_process)
            _current_oauth_record = None
            _current_oauth_process = None
        process, oauth_url = _spawn_oauth_and_parse_url(provider)
        record = _OAuthSessionRecord(
            session_id=_uuid.uuid4().hex, provider=provider, oauth_url=oauth_url
        )
        _current_oauth_record = record
        _current_oauth_process = process
    return OAuthStartResult(session_id=record.session_id, oauth_url=record.oauth_url)


def _drive_oauth_code(process: Any, code: str) -> None:
    process.timeout = _OAUTH_COMPLETE_WAIT_SECONDS
    try:
        process.sendline(code)
        result = process.expect([pexpect.EOF, pexpect.TIMEOUT])
    except pexpect.ExceptionPexpect as e:
        raise ClaudeAuthError(f"claude auth login subprocess failed during code submit: {e}") from e
    if result != 0:
        raise ClaudeAuthError("Timed out waiting for claude auth login to complete after code submit")


def submit_oauth_code(session_id: str, code: str) -> AuthStatus:
    """Send the user's pasted `CODE#STATE` to the live OAuth subprocess."""
    global _current_oauth_record, _current_oauth_process
    with _oauth_lock:
        record = _current_oauth_record
        process = _current_oauth_process
        if record is None or process is None or record.session_id != session_id:
            raise ClaudeAuthError("No active OAuth session matches the provided session_id")
        try:
            _drive_oauth_code(process, code)
        finally:
            # Terminate-then-close runs unconditionally so a timed-out
            # `claude auth login` subprocess doesn't outlive the cleared
            # module-state slot. _safe_terminate is a no-op when the
            # process already reached EOF (the success path), so this is
            # safe on both success and failure branches.
            _safe_terminate(process)
            _safe_close(process)
            _current_oauth_record = None
            _current_oauth_process = None
    return get_auth_status()


def abort_oauth_login() -> None:
    """Drop any in-flight OAuth session (e.g. user closed the modal)."""
    global _current_oauth_record, _current_oauth_process
    with _oauth_lock:
        if _current_oauth_process is not None:
            _safe_terminate(_current_oauth_process)
            _safe_close(_current_oauth_process)
        _current_oauth_record = None
        _current_oauth_process = None
