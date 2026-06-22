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
   subprocess is held on the `ClaudeAuthService` instance between the
   `start_oauth_login` and `submit_oauth_code` calls so the UI can collect
   the code from the user in between.

   The two providers store their credential differently, which dictates
   whether a restart is needed:

   - `--claudeai` writes a subscription credential that every running
     claude re-reads on its next API call -- no restart required.
   - `--console` writes its credential as `primaryApiKey` *inside* the
     shared `$CLAUDE_CONFIG_DIR/.claude.json`. Claude Code reads that file
     once at process start and caches it, so an already-running agent
     never sees the new key. The console path therefore restarts every
     `type: claude` agent (same mechanism as the API-key path below).
2. Raw API key: `submit_api_key` writes `ANTHROPIC_API_KEY` into the host
   env file the bootstrap already manages and then restarts every
   `type: claude` agent in the mind via `mngr stop`/`mngr start`. The
   restart is necessary because env vars are inherited at process start
   and cannot be updated in-place; without it, the new key has no effect
   on already-running claudes.

Both restart paths first run `_prepare_claude_config_for_restart`, which
pre-dismisses the Claude Code startup dialogs (onboarding, theme, custom
API-key challenge) in `.claude.json` so the freshly restarted agents come
up clean instead of blocking on an interactive TUI prompt -- mirroring
what mngr's claude plugin does at agent-creation time. The config edit
runs while every agent is stopped, so no still-running agent clobbers it
from its stale in-memory copy.

Dependencies that touch the outside world (subprocess invocation and
pexpect-driven PTY spawning) are injected into `ClaudeAuthService` at
construction so tests can substitute deterministic fakes without
`unittest.mock` or module-level monkeypatching.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from collections.abc import Callable
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import Any
from typing import Final

import pexpect
from loguru import logger as _loguru_logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import SecretStr

from imbue.concurrency_group.subprocess_utils import ProcessSetupError
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.cli.exit_codes import EXIT_CODE_PROVIDER_INACCESSIBLE
from imbue.mngr.utils.env_utils import parse_env_file
from imbue.mngr_claude.claude_config import acknowledge_cost_threshold
from imbue.mngr_claude.claude_config import complete_onboarding
from imbue.mngr_claude.claude_config import dismiss_effort_callout
from imbue.mngr_claude.claude_config import read_claude_config
from imbue.mngr_claude.resources.stream_snapshot import strip_ansi

logger = _loguru_logger

_HOST_DIR_ENV_VAR = "MNGR_HOST_DIR"
_CLAUDE_CONFIG_DIR_ENV_VAR = "CLAUDE_CONFIG_DIR"
_ANTHROPIC_API_KEY_ENV_VAR = "ANTHROPIC_API_KEY"
# Claude stores per-key approvals keyed by the last 20 characters of the key.
_API_KEY_APPROVAL_SUFFIX_LENGTH: Final = 20
_OAUTH_URL_REGEX = re.compile(r"https://\S*oauth/authorize\S*")
_OAUTH_URL_WAIT_SECONDS: Final = 30.0
_OAUTH_COMPLETE_WAIT_SECONDS: Final = 30.0
_MNGR_COMMAND_TIMEOUT_SECONDS: Final = 60.0
_CLAUDE_AUTH_STATUS_TIMEOUT_SECONDS: Final = 10.0


class ClaudeAuthError(RuntimeError):
    """Raised when an auth flow operation cannot complete."""


# Public type aliases for dependency injection. Tests pass deterministic
# fakes to `ClaudeAuthService`; production code uses the module defaults.
CommandRunner = Callable[..., Any]
PexpectSpawner = Callable[..., Any]


def _default_command_runner(command: list[str], timeout: float, env: Mapping[str, str] | None = None) -> Any:
    return run_local_command_modern_version(command=command, is_checked=False, timeout=timeout, cwd=None, env=env)


def _default_pexpect_spawner(executable: str, args: list[str], timeout: float) -> Any:
    return pexpect.spawn(executable, args, timeout=timeout, encoding="utf-8")


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
    subscription_type: str | None = Field(default=None, description="e.g. 'Max'; absent for Console accounts")


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


def _resolve_claude_config_path() -> Path:
    """Locate the shared `$CLAUDE_CONFIG_DIR/.claude.json` for the mind."""
    config_dir = os.environ.get(_CLAUDE_CONFIG_DIR_ENV_VAR, "")
    if not config_dir:
        raise ClaudeAuthError(f"{_CLAUDE_CONFIG_DIR_ENV_VAR} is unset; cannot locate the Claude config")
    return Path(config_dir) / ".claude.json"


def _approve_api_key_in_claude_config(config_path: Path, api_key: SecretStr) -> None:
    """Add `api_key` to `customApiKeyResponses.approved` in the Claude config.

    Claude Code challenges any `ANTHROPIC_API_KEY` it finds in the
    environment that isn't pre-approved, via an interactive TUI prompt
    that a restarted agent would then block on. Approvals are keyed by the
    last 20 characters of the key (mirrors mngr_claude's
    `approve_api_key_for_claude`). This runs while every agent is stopped,
    so a plain read/write is safe -- no concurrent writer to race.
    """
    config = read_claude_config(config_path)
    responses = config.get("customApiKeyResponses")
    if not isinstance(responses, dict):
        responses = {}
    approved = list(responses.get("approved", []))
    suffix = api_key.get_secret_value()[-_API_KEY_APPROVAL_SUFFIX_LENGTH:]
    if suffix not in approved:
        approved.append(suffix)
    responses["approved"] = approved
    responses.setdefault("rejected", [])
    config["customApiKeyResponses"] = responses
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2) + "\n")


def _prepare_claude_config_for_restart(api_key: SecretStr | None) -> None:
    """Pre-dismiss Claude Code's startup dialogs before agents restart.

    A freshly restarted agent re-runs Claude Code's first-launch flow
    (theme picker, onboarding, custom-API-key challenge). Any of those is
    an interactive TUI prompt that the agent would block on. mngr's claude
    plugin dismisses them at agent-creation time; the modal's restart
    paths must do the same so the recovered agent comes up usable.

    Called between stopping and starting the agents, so the running agents
    cannot clobber the file from their stale in-memory copy.
    """
    config_path = _resolve_claude_config_path()
    complete_onboarding(config_path)
    dismiss_effort_callout(config_path)
    acknowledge_cost_threshold(config_path)
    if api_key is not None:
        _approve_api_key_in_claude_config(config_path, api_key)


def _safe_terminate(process: Any) -> None:
    """Terminate a pexpect spawn without letting teardown errors propagate.

    `pexpect.spawn.isalive()` reaps the child's exit status and wraps
    `ptyprocess` errors in `pexpect.ExceptionPexpect`; `terminate()` can
    raise `OSError` on an already-reaped descriptor. Both live inside the
    try so a half-torn-down process never crashes the caller (called from
    every OAuth teardown path, including the auth-success chokepoint).
    """
    try:
        if not process.isalive():
            return
        process.terminate(force=True)
    except (OSError, pexpect.ExceptionPexpect) as e:
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


def _drive_oauth_code(process: Any, code: str) -> None:
    process.timeout = _OAUTH_COMPLETE_WAIT_SECONDS
    try:
        process.sendline(code)
    except pexpect.ExceptionPexpect as e:
        raise ClaudeAuthError(f"claude auth login subprocess failed sending code: {e}") from e
    try:
        result = process.expect([pexpect.EOF, pexpect.TIMEOUT])
    except pexpect.ExceptionPexpect as e:
        raise ClaudeAuthError(f"claude auth login subprocess failed waiting for completion: {e}") from e
    if result != 0:
        raise ClaudeAuthError("Timed out waiting for claude auth login to complete after code submit")


def _extract_oauth_url(raw_output: str) -> str | None:
    """Pull the single OAuth URL out of `claude auth login`'s PTY output.

    The CLI renders the URL as an OSC 8 terminal hyperlink styled with ANSI
    color, so the raw stream carries the URL *twice* -- once as the
    hyperlink target and once as the styled visible label -- interleaved
    with escape sequences (`ESC]8;;<url>ST <colored url> ESC]8;;ST`).
    Matching the URL straight off that stream captures both copies plus the
    escapes. Stripping the escapes first collapses the hyperlink back to its
    bare visible label, leaving exactly one clean URL for the regex.
    """
    cleaned = strip_ansi(raw_output)
    match = _OAUTH_URL_REGEX.search(cleaned)
    return match.group(0) if match is not None else None


def _build_list_command() -> list[str]:
    """Build the ``mngr list`` argv used to enumerate agents.

    Pure: argv assembly only, so the repo<->mngr CLI contract is testable
    against the live CLI without a subprocess (see ``claude_auth_test.py``).

    ``--on-error continue`` makes this blanket listing tolerate an
    unauthenticated/unreachable provider: ``mngr list`` still emits the
    healthy providers' agents and exits ``EXIT_CODE_PROVIDER_INACCESSIBLE``,
    which the caller treats as success.
    """
    return ["mngr", "list", "--format", "json", "--on-error", "continue"]


def _log_inaccessible_providers(payload: dict[str, Any]) -> None:
    """Debug-log each provider `mngr list` skipped due to an auth/access error.

    The structured `errors` array is present when `mngr list` exits
    EXIT_CODE_PROVIDER_INACCESSIBLE. Skipped providers are expected (e.g. a
    provider enabled in config but never authenticated), so this is debug
    only -- the enumeration still succeeds on the healthy providers.
    """
    errors = payload.get("errors", [])
    if not isinstance(errors, list):
        return
    for error in errors:
        if not isinstance(error, dict):
            continue
        provider_name = error.get("provider_name", "?")
        message = error.get("message", "")
        logger.debug("Skipped inaccessible provider {} while listing agents: {}", provider_name, message)


def _build_stop_command(name: str) -> list[str]:
    """Build the ``mngr stop`` argv for one agent. Pure (see above)."""
    return ["mngr", "stop", name]


def _build_start_command(name: str) -> list[str]:
    """Build the ``mngr start --no-resume`` argv for one agent. Pure (see above)."""
    return ["mngr", "start", "--no-resume", name]


class ClaudeAuthService(MutableModel):
    """Stateful entry point for the in-mind Claude auth-recovery flows.

    Holds the injected `command_runner` / `pexpect_spawner` dependencies
    and the in-flight OAuth subprocess. One instance is created per
    application and stored on `app.state`; the OAuth subprocess held
    between `start_oauth_login` and `submit_oauth_code` rides that
    instance. Tests construct isolated instances with deterministic fakes.
    """

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    command_runner: CommandRunner = _default_command_runner
    pexpect_spawner: PexpectSpawner = _default_pexpect_spawner

    # Only one OAuth flow can be live at a time per instance, which matches
    # the single-mind / single-user deployment model. The lock and the live
    # subprocess are private runtime state, not configuration data.
    _oauth_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _current_oauth_record: _OAuthSessionRecord | None = PrivateAttr(default=None)
    _current_oauth_process: Any = PrivateAttr(default=None)

    def get_auth_status(self, extra_env: Mapping[str, str] | None = None) -> AuthStatus:
        """Invoke `claude auth status --json` and parse the result.

        Returns `logged_in=False` if the `claude` binary is missing or
        doesn't produce output, rather than raising, since the whole point
        of the modal is to recover from broken auth state.

        `extra_env` is overlaid on the current environment for the status
        subprocess. The API-key path needs this: `submit_api_key` writes
        `ANTHROPIC_API_KEY` to the host env *file*, but the long-lived
        system-interface process that runs this check never received that
        variable, so without overlaying the key here `claude auth status`
        would report `loggedIn=false` for a perfectly valid key.
        """
        runner_env = {**os.environ, **extra_env} if extra_env is not None else None
        try:
            result = (
                self.command_runner(
                    ["claude", "auth", "status", "--json"],
                    _CLAUDE_AUTH_STATUS_TIMEOUT_SECONDS,
                    runner_env,
                )
                if runner_env is not None
                else self.command_runner(["claude", "auth", "status", "--json"], _CLAUDE_AUTH_STATUS_TIMEOUT_SECONDS)
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

    def list_claude_agent_names(self) -> list[str]:
        """Return the names of every `type: claude` agent in the local mind.

        Uses `mngr list --format json` and filters to `type == "claude"`.
        This excludes the `main`-type system-services agent, which has no
        interactive claude process to restart.
        """
        result = self.command_runner(_build_list_command(), _MNGR_COMMAND_TIMEOUT_SECONDS)
        # Exit EXIT_CODE_PROVIDER_INACCESSIBLE means some enabled provider was
        # unauthenticated/unreachable, but the healthy providers' agents were
        # still listed (we pass --on-error continue). This is a blanket listing,
        # so that is an acceptable partial success: enumerate what we got. Any
        # other nonzero exit is a real failure.
        if result.returncode not in (0, EXIT_CODE_PROVIDER_INACCESSIBLE):
            raise ClaudeAuthError(f"mngr list failed (exit {result.returncode}): {result.stderr.strip()}")
        stdout = result.stdout if isinstance(result.stdout, str) else ""
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as e:
            raise ClaudeAuthError(f"mngr list returned non-JSON output: {stdout!r}") from e
        if not isinstance(payload, dict):
            raise ClaudeAuthError(f"mngr list returned non-object JSON: {payload!r}")
        if result.returncode == EXIT_CODE_PROVIDER_INACCESSIBLE:
            _log_inaccessible_providers(payload)
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

    def restart_all_claude_agents(self, api_key: SecretStr | None = None) -> list[str]:
        """Restart every `type: claude` agent via `mngr stop` then `mngr start`.

        Stops every agent first, then prepares the shared Claude config
        (see `_prepare_claude_config_for_restart`), then starts them again.
        The stop-all/prepare/start-all ordering matters: editing
        `.claude.json` while an agent is still running would be silently
        overwritten by that agent's stale in-memory copy on its next write.

        Agents are started with `--no-resume` so mngr does not deliver the
        configured resume message (e.g. "Continue from where you left
        off") after the restart -- an auth recovery is not a work
        interruption the agent should pick back up from, and that message
        would otherwise appear as a spurious turn in the chat.

        `api_key`, when given, is additionally approved in the Claude
        config so the API-key auth path's freshly-written key doesn't trip
        Claude's custom-key challenge.

        Returns the list of agent names that were restarted.
        """
        names = self.list_claude_agent_names()
        for name in names:
            logger.info("Stopping type:claude agent {} via mngr stop", name)
            stop_result = self.command_runner(_build_stop_command(name), _MNGR_COMMAND_TIMEOUT_SECONDS)
            if stop_result.returncode != 0:
                raise ClaudeAuthError(
                    f"mngr stop {name} failed (exit {stop_result.returncode}): {stop_result.stderr.strip()}"
                )
        _prepare_claude_config_for_restart(api_key)
        for name in names:
            logger.info("Starting type:claude agent {} via mngr start --no-resume", name)
            start_result = self.command_runner(_build_start_command(name), _MNGR_COMMAND_TIMEOUT_SECONDS)
            if start_result.returncode != 0:
                raise ClaudeAuthError(
                    f"mngr start {name} failed (exit {start_result.returncode}): {start_result.stderr.strip()}"
                )
        return names

    def submit_api_key(self, api_key: SecretStr) -> AuthStatus:
        """Write `ANTHROPIC_API_KEY` to host env then restart every claude agent.

        All `type: claude` agents must be restarted: env vars are read at
        process start, so already-running claudes won't pick up the new key
        until their tmux sessions are torn down and respawned. The key is
        also passed to the restart so it gets pre-approved in the Claude
        config.

        The final status check overlays `ANTHROPIC_API_KEY` onto the
        subprocess environment: the key was written to the host env *file*,
        which the long-lived system-interface process never sourced, so a
        plain `claude auth status` would report `loggedIn=false` for a
        valid key and the modal would wrongly tell the user it was
        rejected.
        """
        write_api_key_to_host_env(api_key)
        self.restart_all_claude_agents(api_key=api_key)
        return self.get_auth_status(extra_env={_ANTHROPIC_API_KEY_ENV_VAR: api_key.get_secret_value()})

    def _spawn_oauth_and_parse_url(self, provider: OAuthProvider) -> tuple[Any, str]:
        process = self.pexpect_spawner(
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
        # `process.match` spans the raw stream, where the CLI's OSC 8
        # hyperlink renders the URL twice and wraps it in escape sequences.
        # Re-extract from the full consumed buffer (`before` + `after`, which
        # together hold the hyperlink's opening `ESC]8;;` and both URL copies)
        # with the escapes stripped, so we hand back one clean URL.
        consumed = (process.before or "") + (process.after or "")
        oauth_url = _extract_oauth_url(consumed)
        if oauth_url is None:
            _safe_terminate(process)
            _safe_close(process)
            raise ClaudeAuthError(
                "OAuth URL matched in the stream but could not be extracted after stripping terminal escape sequences"
            )
        return process, oauth_url

    def start_oauth_login(self, provider: OAuthProvider) -> OAuthStartResult:
        """Spawn `claude auth login --<provider>` and return the parsed OAuth URL.

        Replaces any prior in-flight session: only one OAuth flow can be
        live at a time per instance, which matches the single-mind /
        single-user deployment model.
        """
        with self._oauth_lock:
            if self._current_oauth_process is not None:
                _safe_terminate(self._current_oauth_process)
                _safe_close(self._current_oauth_process)
                self._current_oauth_record = None
                self._current_oauth_process = None
            process, oauth_url = self._spawn_oauth_and_parse_url(provider)
            record = _OAuthSessionRecord(session_id=uuid.uuid4().hex, provider=provider, oauth_url=oauth_url)
            self._current_oauth_record = record
            self._current_oauth_process = process
        return OAuthStartResult(session_id=record.session_id, oauth_url=record.oauth_url)

    def submit_oauth_code(self, session_id: str, code: str) -> AuthStatus:
        """Send the user's pasted `CODE#STATE` to the live OAuth subprocess.

        The Console (`--console`) provider writes its credential as
        `primaryApiKey` inside the cached `.claude.json`, which an
        already-running agent never re-reads -- so the console path
        restarts every `type: claude` agent once the login completes. The
        subscription (`--claudeai`) provider's credential is re-read live,
        so it skips the restart.
        """
        with self._oauth_lock:
            record = self._current_oauth_record
            process = self._current_oauth_process
            if record is None or process is None or record.session_id != session_id:
                raise ClaudeAuthError("No active OAuth session matches the provided session_id")
            provider = record.provider
            try:
                _drive_oauth_code(process, code)
            finally:
                # Terminate-then-close runs unconditionally so a timed-out
                # `claude auth login` subprocess doesn't outlive the cleared
                # instance-state slot. _safe_terminate is a no-op when the
                # process already reached EOF (the success path), so this
                # is safe on both success and failure branches.
                _safe_terminate(process)
                _safe_close(process)
                self._current_oauth_record = None
                self._current_oauth_process = None
        if provider is OAuthProvider.CONSOLE:
            self.restart_all_claude_agents()
        return self.get_auth_status()

    def abort_oauth_login(self) -> None:
        """Drop any in-flight OAuth session (e.g. user closed the modal)."""
        with self._oauth_lock:
            if self._current_oauth_process is not None:
                _safe_terminate(self._current_oauth_process)
                _safe_close(self._current_oauth_process)
            self._current_oauth_record = None
            self._current_oauth_process = None
