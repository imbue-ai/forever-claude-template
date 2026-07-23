"""In-mind Claude authentication: settings-env credential writes, setup-token flow, agent restarts.

Implements the backend half of the in-UI Claude login modal. All credentials
live in the ``env`` block of the shared ``$CLAUDE_CONFIG_DIR/settings.json``
(the config dir every claude in the mind inherits), NEVER in the mngr host
env file: the host env file is frozen into long-lived processes (supervisord
and its services) at boot, so changing it would require tearing down the
whole workspace, while a settings.json edit only requires restarting the
claude agents themselves.

Five sign-in paths:

1. Subscription (primary): `claude auth login --claudeai` is driven via
   pexpect. The CLI prints an `oauth/authorize` URL, the user approves in
   the browser and pastes the shown code (the CLI can also complete on its
   own via its polling). The credential is stored by the CLI itself and
   running claudes re-read it on their next API call, so a fresh workspace
   signs in with NO restart. Managed settings-env keys outrank this
   credential, so when any are active the sign-in clears them and restarts
   the agents (the switching case).
2. Raw API key: written as ``ANTHROPIC_API_KEY`` into the settings env.
3. Imbue (LiteLLM): an env-var-style blob pasted from the desktop app's
   mint page, written as ``ANTHROPIC_API_KEY`` + ``ANTHROPIC_BASE_URL``.
4. Long-lived token: `claude setup-token` via the same PTY machinery; the
   minted 1-year token is written as ``CLAUDE_CODE_OAUTH_TOKEN``.
5. Anthropic Console: `claude auth login --console`; its key lands inside
   `.claude.json` (cached at claude process start), so it always clears
   the managed keys and restarts the agents.

Paths 2 and 3 (and a subtle "paste an existing token" affordance) share one
strict env-lines parser: only the three managed keys are accepted, and
mixed-mode pastes (an OAuth token alongside an API key) are rejected so the
written state is always unambiguous. The writer fully controls the managed
keys -- switching modes deletes the other mode's keys.

Every successful write restarts the mind's claude-binary agents (types
``claude`` AND ``worker``; the ``main`` services agent is excluded -- its
window 0 never runs a live claude, and restarting it would tear down
supervisord and every background service). Settings-env values are read at
claude process start, so a restart is what makes new credentials take
effect. Agent states are snapshotted (via ``mngr list``) before stopping:
agents that were RUNNING mid-task get a "please continue" message after the
restart so unattended workers resume instead of silently dying; WAITING
agents need nothing (their next user message starts them with the fresh
env); STOPPED agents are left stopped.

Restarts touch nothing outside the settings env block: settings-env
credentials do not trip Claude Code's API-key challenge, and the startup
dialog dismissals in `.claude.json` are guaranteed by mngr's claude
plugin at agent-creation time. There is deliberately no pre-restart
credential probe: a bad credential surfaces on the agent's first request,
where the transcript auth-error detection reopens the modal.

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
import time
import uuid
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from enum import Enum
from pathlib import Path
from typing import Any
from typing import Final

import pexpect
import pyte
from loguru import logger as _loguru_logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.subprocess_utils import ProcessSetupError
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mngr.cli.exit_codes import EXIT_CODE_PROVIDER_INACCESSIBLE
from imbue.mngr.utils.env_utils import parse_env_file

logger = _loguru_logger

_CLAUDE_CONFIG_DIR_ENV_VAR = "CLAUDE_CONFIG_DIR"
_HOST_DIR_ENV_VAR = "MNGR_HOST_DIR"
ANTHROPIC_API_KEY_ENV_VAR: Final[str] = "ANTHROPIC_API_KEY"
ANTHROPIC_BASE_URL_ENV_VAR: Final[str] = "ANTHROPIC_BASE_URL"
CLAUDE_CODE_OAUTH_TOKEN_ENV_VAR: Final[str] = "CLAUDE_CODE_OAUTH_TOKEN"
# The full set of settings-env keys this module owns. The writer enforces
# both presence AND absence: every write deletes all three before setting
# the submitted subset, so stale keys from a previous mode can never
# shadow the new one (ANTHROPIC_API_KEY outranks CLAUDE_CODE_OAUTH_TOKEN
# in Claude Code's credential precedence, so a leftover key would
# silently win over a freshly written token).
MANAGED_AUTH_ENV_KEYS: Final[frozenset[str]] = frozenset(
    (ANTHROPIC_API_KEY_ENV_VAR, ANTHROPIC_BASE_URL_ENV_VAR, CLAUDE_CODE_OAUTH_TOKEN_ENV_VAR)
)
# Characters of the key/token shown in the modal's "currently signed in via"
# header; long enough to disambiguate, short enough to stay a non-secret.
_DISPLAY_SUFFIX_LENGTH: Final = 4
# Fires on the first sight of the OAuth URL in the PTY stream. This is only a
# *trigger*: the CLI's Ink renderer hard-wraps the visible URL at the terminal
# width (pexpect's default PTY is 80 columns) and pexpect can match mid
# render-frame, so the buffer may hold just a prefix. The actual URL is
# recovered by `_extract_oauth_url` after draining the stream.
_OAUTH_URL_REGEX = re.compile(r"https://\S*oauth/authorize\S*")
# An OSC 8 terminal hyperlink: `ESC ] 8 ; params ; target (BEL | ESC \)`.
# The params field is not always empty (the CLI emits `id=...`). The target
# carries the full URL with no width-wrapping, so it survives narrow PTYs
# that hard-wrap the visible label.
_OSC8_HYPERLINK_REGEX = re.compile(r"\x1b\]8;[^;\x07\x1b]*;([^\x07\x1b]+)(?:\x07|\x1b\\)")
# Strict charset for re-assembling a width-wrapped URL from visible text:
# unlike `\S`, it excludes stray control bytes left between render fragments.
_OAUTH_URL_CHARSET = r"[A-Za-z0-9%&=?_.~/:+#-]"
_OAUTH_URL_STRICT_REGEX = re.compile(rf"https://{_OAUTH_URL_CHARSET}*oauth/authorize{_OAUTH_URL_CHARSET}*")
_OAUTH_URL_CONTINUATION_REGEX = re.compile(rf"^{_OAUTH_URL_CHARSET}+$")
# End-of-frame marker for Ink's synchronized-update rendering; the replay
# in _extract_wrapped_value snapshots the screen at each of these.
_FRAME_END_MARKER: Final = "\x1b[?2026l"
# The PTY geometry used for `claude setup-token`. Pinned explicitly on the
# spawn AND used to replay the stream through the terminal emulator during
# extraction -- the two must match or the reconstructed screen's wrapping
# would not correspond to what the CLI rendered.
_PTY_LINES: Final = 24
_PTY_COLUMNS: Final = 80
# The long-lived token `claude setup-token` prints on completion. Like the
# URL regex, only a trigger -- extraction re-assembles the possibly
# width-wrapped token from the drained stream.
_SETUP_TOKEN_REGEX = re.compile(r"sk-ant-oat01-[A-Za-z0-9_-]+")
_SETUP_TOKEN_STRICT_REGEX = re.compile(r"sk-ant-oat01-[A-Za-z0-9_-]*")
_SETUP_TOKEN_CONTINUATION_REGEX = re.compile(r"^[A-Za-z0-9_-]+$")
# Printed by the CLI when Anthropic rejects a pasted code (wrong, expired, or
# from an earlier attempt's state) or its own polling hits an error; the CLI
# then parks on a "Press Enter to retry." prompt, so without failing fast the
# session would just time out with a misleading message.
_OAUTH_ERROR_REGEX = re.compile(r"OAuth error")
# Printed plainly (outside the Ink renderer) by `claude auth login` right
# before it exits 0 / 1 respectively, so no screen replay is needed to
# detect completion of the credentials-based browser sign-ins.
_LOGIN_SUCCESS_REGEX = re.compile(r"Login successful")
_LOGIN_FAILED_LINE_REGEX = re.compile(r"Login failed: ?([^\r\n]*)")
# The CLI's Ink input treats a rapid burst of characters as a paste; Enter
# must arrive as its own later keystroke or it lands in the field as
# content. The burst is over once the input echo goes quiet for
# _CODE_ECHO_QUIET_SECONDS (deadline-capped so a silent PTY cannot stall
# the submit).
_CODE_ECHO_QUIET_SECONDS: Final = 0.3
_CODE_ECHO_DEADLINE_SECONDS: Final = 3.0
# Real setup tokens are ~110 characters. A much shorter extraction is a
# wrapped fragment, not the token -- keep waiting rather than storing it.
_MIN_SETUP_TOKEN_LENGTH: Final = 60
# After a trigger regex fires, keep draining the PTY until the caller's
# completion predicate is satisfied or EOF; this hard deadline is only a
# hang backstop (generous: the token path drains to process exit).
_STREAM_DRAIN_DEADLINE_SECONDS: Final = 15.0
_STREAM_DRAIN_READ_SECONDS: Final = 0.25
_OAUTH_URL_WAIT_SECONDS: Final = 30.0
_SETUP_TOKEN_POLL_SECONDS: Final = 0.2
_SETUP_TOKEN_CODE_WAIT_SECONDS: Final = 30.0
_MNGR_COMMAND_TIMEOUT_SECONDS: Final = 60.0
# A fused `mngr start --restart` call stops, starts, readiness-waits, and
# (for previously-RUNNING agents) messages a whole batch of agents. It runs
# on the background restart thread, so the generous ceiling costs nothing
# in the request path.
_MNGR_RESTART_TIMEOUT_SECONDS: Final = 600.0
_CLAUDE_AUTH_STATUS_TIMEOUT_SECONDS: Final = 10.0

# Agent types whose window-0 process is a real claude binary and therefore
# holds credentials frozen from process start. The `main` services agent is
# deliberately absent: its window 0 sleeps forever and restarting it would
# tear down supervisord and every background service.
CLAUDE_BINARY_AGENT_TYPES: Final[frozenset[str]] = frozenset(("claude", "worker"))
_AGENT_STATE_RUNNING: Final[str] = "RUNNING"
_AGENT_STATE_WAITING: Final[str] = "WAITING"

# Sent (via `mngr message`) to agents that were RUNNING when the auth-change
# restart tore them down, so unattended work resumes instead of silently
# stopping. WAITING agents are not messaged: their next user message starts
# them under the fresh env anyway.
RESTART_CONTINUE_MESSAGE: Final[str] = (
    "Your Claude credentials were just updated and your session was restarted. "
    "Please continue what you were working on."
)


class ClaudeAuthError(RuntimeError):
    """Raised when an auth flow operation cannot complete."""


class CredentialPasteError(ClaudeAuthError):
    """Raised when a pasted credential blob fails strict validation."""


# Public type aliases for dependency injection. Tests pass deterministic
# fakes to `ClaudeAuthService`; production code uses the module defaults.
CommandRunner = Callable[..., Any]
PexpectSpawner = Callable[..., Any]


def _default_command_runner(command: list[str], timeout: float, env: Mapping[str, str] | None = None) -> Any:
    return run_local_command_modern_version(command=command, is_checked=False, timeout=timeout, cwd=None, env=env)


def _default_pexpect_spawner(executable: str, args: list[str], timeout: float) -> Any:
    # Dimensions pinned to the geometry the extraction replays the stream
    # at (see _render_final_screen) -- these are pexpect's defaults, made
    # explicit so the two can never drift apart.
    return pexpect.spawn(
        executable, args, timeout=timeout, encoding="utf-8", dimensions=(_PTY_LINES, _PTY_COLUMNS)
    )


class AuthMode(str, Enum):
    """The workspace's effective auth mode.

    Derived from the managed settings-env keys when any are present; with
    an empty managed env, folded from `claude auth status` so the
    credentials-based browser sign-ins (subscription and Console) surface
    correctly.
    """

    SUBSCRIPTION = "subscription"
    CONSOLE = "console"
    IMBUE = "imbue"
    API_KEY = "api_key"
    NONE = "none"


class OAuthProvider(str, Enum):
    """Which `claude auth login` provider a browser sign-in session targets."""

    CLAUDEAI = "claudeai"
    CONSOLE = "console"


class AuthFlowKind(str, Enum):
    """Which PTY-driven auth flow an in-flight session is running."""

    SETUP_TOKEN = "setup_token"
    OAUTH_LOGIN = "oauth_login"


class RestartReason(str, Enum):
    """Why the background agent restart is running (drives the checklist copy)."""

    CREDENTIALS_SAVED = "credentials_saved"
    SUBSCRIPTION_SWITCH = "subscription_switch"
    CONSOLE_SWITCH = "console_switch"


class AuthStatus(FrozenModel):
    """Parsed output of `claude auth status --json`, plus the derived mode.

    On the pinned Claude Code version, both browser sign-ins report
    `authMethod: "claude.ai"` (the Console-stored key resolves through the
    "/login managed key" source); `subscription_type` is present only for
    subscription accounts, which is the discriminator. It is also unset
    for setup-token (`oauth_token`) sessions.
    """

    logged_in: bool = Field(description="Whether claude is currently authenticated")
    auth_method: str | None = Field(
        default=None, description="e.g. 'claude.ai', 'api_key', 'oauth_token', 'api_key_helper', 'none'"
    )
    api_provider: str | None = Field(default=None, description="e.g. 'anthropic', 'claudeai', 'firstParty'")
    email: str | None = Field(default=None)
    org_id: str | None = Field(default=None)
    org_name: str | None = Field(default=None)
    subscription_type: str | None = Field(default=None, description="e.g. 'Max'; absent for token/Console sessions")
    auth_mode: AuthMode = Field(default=AuthMode.NONE, description="The workspace's effective auth mode")
    masked_key_suffix: str | None = Field(
        default=None, description="Last few characters of the managed key/token, for display"
    )
    workspace_host_id: str | None = Field(
        default=None, description="This mind's mngr host id, for the desktop app's key-mint page link"
    )
    restart_phase: str | None = Field(
        default=None, description="Phase of the post-auth agent restart: 'restarting', 'finishing', 'done', 'failed'"
    )
    restart_detail: str | None = Field(default=None, description="Human-readable detail for the current restart phase")
    restart_error: str | None = Field(default=None, description="Error message when restart_phase is 'failed'")
    restart_reason: str | None = Field(
        default=None, description="Why the restart is running: 'credentials_saved', 'subscription_switch', 'console_switch'"
    )


class RestartPhase(str, Enum):
    """Lifecycle of the background credential apply that follows an auth change."""

    RESTARTING = "restarting"
    FINISHING = "finishing"
    DONE = "done"
    FAILED = "failed"


class RestartProgress(FrozenModel):
    """Snapshot of the background agent restart's progress."""

    phase: RestartPhase = Field(description="Current phase of the restart")
    detail: str | None = Field(default=None, description="Human-readable detail for the phase")
    error: str | None = Field(default=None, description="Error message when the phase is FAILED")
    reason: RestartReason = Field(description="Why the restart is running (drives the checklist copy)")


class AuthFlowStartResult(FrozenModel):
    """Result of spawning a PTY auth flow (`claude setup-token` or `claude auth login`)."""

    session_id: str = Field(description="Opaque token for the in-flight session")
    oauth_url: str = Field(description="URL the user opens to authorize the login")


class AuthFlowPollResult(FrozenModel):
    """Result of polling an in-flight PTY auth flow."""

    is_complete: bool = Field(description="Whether the flow completed and was applied")
    status: AuthStatus | None = Field(default=None, description="Auth status after completion; None while pending")


class _AuthFlowSessionRecord(FrozenModel):
    """Immutable handle for an in-flight PTY auth subprocess.

    Pairs with a parallel non-frozen slot that holds the live pexpect
    process object, since that object is not Pydantic-serializable.
    """

    session_id: str
    kind: AuthFlowKind
    provider: OAuthProvider | None
    oauth_url: str


class AgentSnapshot(FrozenModel):
    """One claude-binary agent's name and lifecycle state at snapshot time."""

    name: str = Field(description="Agent name (used to address mngr stop/start/message)")
    state: str = Field(description="Lifecycle state string from mngr list (e.g. 'RUNNING', 'WAITING')")


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


@pure
def parse_credential_lines(pasted_text: str) -> dict[str, str]:
    """Parse a pasted env-var-style credential blob into the managed keys.

    Strict by design: the settings env block is fully controlled, so a paste
    is rejected (rather than partially applied) when it contains any key
    outside the managed set, mixes an OAuth token with an API key (the key
    would silently outrank the token at runtime), supplies a base URL with
    no key, or contains no managed key at all.

    Raises CredentialPasteError with a user-facing message on any violation.
    """
    parsed = parse_env_file(pasted_text)
    stripped = {key: value.strip() for key, value in parsed.items() if value.strip()}
    if not stripped:
        raise CredentialPasteError("No credentials found. Paste lines like ANTHROPIC_API_KEY=sk-ant-...")
    unknown_keys = sorted(set(stripped) - MANAGED_AUTH_ENV_KEYS)
    if unknown_keys:
        raise CredentialPasteError(
            "Unsupported keys in paste: {}. Only {} are accepted.".format(
                ", ".join(unknown_keys), ", ".join(sorted(MANAGED_AUTH_ENV_KEYS))
            )
        )
    has_token = CLAUDE_CODE_OAUTH_TOKEN_ENV_VAR in stripped
    has_key = ANTHROPIC_API_KEY_ENV_VAR in stripped
    has_base_url = ANTHROPIC_BASE_URL_ENV_VAR in stripped
    if has_token and (has_key or has_base_url):
        raise CredentialPasteError(
            "Paste either an OAuth token OR an API key (with optional base URL), not both: "
            "an API key would silently take precedence over the token."
        )
    if has_base_url and not has_key:
        raise CredentialPasteError(
            f"{ANTHROPIC_BASE_URL_ENV_VAR} requires an accompanying {ANTHROPIC_API_KEY_ENV_VAR}."
        )
    return stripped


@pure
def derive_auth_mode(managed_env: Mapping[str, str]) -> AuthMode:
    """Derive the auth mode implied by the managed settings-env keys.

    Mirrors Claude Code's credential precedence: an API key outranks an
    OAuth token, and a key paired with a base URL means requests route to
    a proxy (the Imbue LiteLLM case).
    """
    if managed_env.get(ANTHROPIC_API_KEY_ENV_VAR):
        if managed_env.get(ANTHROPIC_BASE_URL_ENV_VAR):
            return AuthMode.IMBUE
        return AuthMode.API_KEY
    elif managed_env.get(CLAUDE_CODE_OAUTH_TOKEN_ENV_VAR):
        return AuthMode.SUBSCRIPTION
    else:
        return AuthMode.NONE


@pure
def masked_credential_suffix(managed_env: Mapping[str, str]) -> str | None:
    """Last few characters of the active managed credential, for display."""
    credential = managed_env.get(ANTHROPIC_API_KEY_ENV_VAR) or managed_env.get(CLAUDE_CODE_OAUTH_TOKEN_ENV_VAR)
    if not credential:
        return None
    return credential[-_DISPLAY_SUFFIX_LENGTH:]


def read_workspace_host_id() -> str | None:
    """Read this mind's mngr host id from `$MNGR_HOST_DIR/data.json`.

    Tolerant: returns None when the env var or file is missing/corrupt --
    the host id only powers the desktop app's key-mint page link, and the
    rest of the modal must keep working without it.
    """
    host_dir = os.environ.get(_HOST_DIR_ENV_VAR, "")
    if not host_dir:
        return None
    data_path = Path(host_dir) / "data.json"
    if not data_path.exists():
        return None
    try:
        data = json.loads(data_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Cannot read host data.json at {}: {}", data_path, e)
        return None
    host_id = data.get("host_id") if isinstance(data, dict) else None
    return host_id if isinstance(host_id, str) and host_id else None


def _resolve_claude_config_dir() -> Path:
    config_dir = os.environ.get(_CLAUDE_CONFIG_DIR_ENV_VAR, "")
    if not config_dir:
        raise ClaudeAuthError(f"{_CLAUDE_CONFIG_DIR_ENV_VAR} is unset; cannot locate the Claude config")
    return Path(config_dir)


def _resolve_claude_settings_path() -> Path:
    """Locate the shared `$CLAUDE_CONFIG_DIR/settings.json` for the mind."""
    return _resolve_claude_config_dir() / "settings.json"


def read_managed_auth_env(settings_path_override: Path | None = None) -> dict[str, str]:
    """Read the managed auth keys currently in the shared settings.json env block."""
    settings_path = settings_path_override or _resolve_claude_settings_path()
    if not settings_path.exists():
        return {}
    try:
        settings = json.loads(settings_path.read_text())
    except json.JSONDecodeError as e:
        logger.warning("Corrupt settings.json at {}: {}", settings_path, e)
        return {}
    if not isinstance(settings, dict):
        logger.warning("Non-object settings.json at {}", settings_path)
        return {}
    env = settings.get("env")
    if not isinstance(env, dict):
        return {}
    return {key: str(value) for key, value in env.items() if key in MANAGED_AUTH_ENV_KEYS and isinstance(value, str)}


def write_managed_auth_env(managed_env: Mapping[str, str], settings_path_override: Path | None = None) -> Path:
    """Write the managed auth keys into the shared settings.json env block.

    Fully controlled: every managed key absent from `managed_env` is DELETED
    from the env block, so a mode switch can never leave a stale credential
    behind to shadow the new one. Non-managed env keys and every other
    setting are preserved untouched.
    """
    for key in managed_env:
        if key not in MANAGED_AUTH_ENV_KEYS:
            raise ClaudeAuthError(f"Refusing to write unmanaged settings env key {key!r}")
    settings_path = settings_path_override or _resolve_claude_settings_path()
    settings: dict[str, Any] = {}
    if settings_path.exists():
        try:
            loaded = json.loads(settings_path.read_text())
        except json.JSONDecodeError as e:
            # A corrupt shared settings file would break every claude in the
            # mind well beyond auth; refuse to silently replace it.
            raise ClaudeAuthError(f"Shared Claude settings at {settings_path} are corrupt JSON: {e}") from e
        if not isinstance(loaded, dict):
            raise ClaudeAuthError(f"Shared Claude settings at {settings_path} are not a JSON object")
        settings = loaded
    env = settings.get("env")
    if not isinstance(env, dict):
        env = {}
    preserved = {key: value for key, value in env.items() if key not in MANAGED_AUTH_ENV_KEYS}
    updated_env = {**preserved, **dict(managed_env)}
    if updated_env:
        settings["env"] = updated_env
    else:
        settings.pop("env", None)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    logger.info("Wrote managed auth env ({} mode) to {}", derive_auth_mode(managed_env).value, settings_path)
    return settings_path


def _safe_terminate(process: Any) -> None:
    """Terminate a pexpect spawn without letting teardown errors propagate.

    `pexpect.spawn.isalive()` reaps the child's exit status and wraps
    `ptyprocess` errors in `pexpect.ExceptionPexpect`; `terminate()` can
    raise `OSError` on an already-reaped descriptor. Both live inside the
    try so a half-torn-down process never crashes the caller (called from
    every setup-token teardown path, including the auth-success chokepoint).
    """
    try:
        if not process.isalive():
            return
        process.terminate(force=True)
    except (OSError, pexpect.ExceptionPexpect) as e:
        logger.warning("setup-token subprocess terminate raised: {}", e)


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
        logger.warning("setup-token subprocess close raised: {}", e)


@pure
def _extract_value_from_screen_rows(
    rows: list[str],
    start_regex: re.Pattern[str],
    continuation_regex: re.Pattern[str],
) -> tuple[str, bool] | None:
    """Find `start_regex` on one rendered screen, de-wrapping across rows.

    A value hard-wrapped by the renderer occupies its row through the last
    column and continues on the next row; a row with trailing blank space
    is the value's final row. Rows arrive space-padded to the full screen
    width (pyte's display invariant), which the wrap detection relies on.

    Returns the value plus whether it provably *ended*: a full-width row
    with only blank space under it is ambiguous (the continuation may not
    have been drawn yet on this frame), so only non-continuation content
    under the row proves the value ended at the screen edge.
    """
    for idx, row in enumerate(rows):
        match = start_regex.search(row)
        if match is None:
            continue
        value = row[match.start() :].rstrip()
        row_idx = idx
        # A row whose last column is occupied wrapped onto the next row.
        while rows[row_idx].rstrip() and len(rows[row_idx].rstrip()) == len(rows[row_idx]):
            candidate = rows[row_idx + 1].strip() if row_idx + 1 < len(rows) else ""
            if candidate == "":
                return value, False
            if continuation_regex.match(candidate) is None:
                return value, True
            value += candidate
            row_idx += 1
        return value, True
    return None


@pure
def _extract_wrapped_value(
    raw_output: str,
    start_regex: re.Pattern[str],
    continuation_regex: re.Pattern[str],
) -> str | None:
    """Recover a possibly width-wrapped value from a raw PTY stream.

    The CLI's Ink renderer emits diff-based frames full of cursor
    positioning, so the raw stream's byte order does not correspond to the
    visual layout -- only a terminal-emulator replay at the exact PTY
    geometry recovers what was actually on screen. The stream is replayed
    frame by frame (split on the synchronized-update end marker Ink emits
    after each frame) and the longest provably-terminated candidate across
    ALL frames wins: a single mid-frame screen can show a truncated prefix
    over the previous frame's stale content, and the final screen alone
    can miss the value entirely if the CLI clears it on exit. A truncated
    candidate is a strict prefix of the real one, so longest-wins selects
    the fully drawn frame.
    """
    screen = pyte.Screen(_PTY_COLUMNS, _PTY_LINES)
    stream = pyte.Stream(screen)
    best_terminated: str | None = None
    best_any: str | None = None
    for frame_chunk in raw_output.split(_FRAME_END_MARKER):
        stream.feed(frame_chunk)
        extracted = _extract_value_from_screen_rows(list(screen.display), start_regex, continuation_regex)
        if extracted is None:
            continue
        value, is_terminated = extracted
        if best_any is None or len(value) > len(best_any):
            best_any = value
        if is_terminated and (best_terminated is None or len(value) > len(best_terminated)):
            best_terminated = value
    return best_terminated if best_terminated is not None else best_any


@pure
def _extract_oauth_url_from_hyperlink(raw_output: str) -> str | None:
    """Pull the OAuth URL from an OSC 8 hyperlink target in the raw stream.

    The CLI renders the URL as an OSC 8 terminal hyperlink; the (invisible)
    target carries the full URL with no width-wrapping, unlike the visible
    label, which Ink hard-wraps at the terminal width. Only *terminated*
    sequences match, so a half-received target is never returned.
    """
    for match in _OSC8_HYPERLINK_REGEX.finditer(raw_output):
        target_match = _OAUTH_URL_STRICT_REGEX.search(match.group(1))
        if target_match is not None:
            return target_match.group(0)
    return None


@pure
def _extract_oauth_url(raw_output: str) -> str | None:
    """Pull the single OAuth URL out of `claude setup-token`'s PTY output.

    Prefers the OSC 8 hyperlink target (complete by construction); falls
    back to re-assembling the width-wrapped visible label when the CLI did
    not emit a hyperlink.
    """
    from_hyperlink = _extract_oauth_url_from_hyperlink(raw_output)
    if from_hyperlink is not None:
        return from_hyperlink
    return _extract_wrapped_value(raw_output, _OAUTH_URL_STRICT_REGEX, _OAUTH_URL_CONTINUATION_REGEX)


@pure
def _extract_setup_token(raw_output: str) -> str | None:
    """Pull the minted `sk-ant-oat01-...` token out of the PTY output.

    The token is longer than an 80-column row, so it may be width-wrapped
    just like the OAuth URL (but has no hyperlink copy). A too-short
    extraction is a wrapped fragment, not the token -- return None so the
    caller keeps draining instead of storing a truncated token.
    """
    token = _extract_wrapped_value(raw_output, _SETUP_TOKEN_STRICT_REGEX, _SETUP_TOKEN_CONTINUATION_REGEX)
    if token is None or len(token) < _MIN_SETUP_TOKEN_LENGTH:
        return None
    return token


def _drain_pty_stream_until_quiet(
    process: Any, consumed: str, quiet_seconds: float, deadline_seconds: float
) -> str:
    """Read PTY output until no chunk arrives for `quiet_seconds`.

    Used to detect the end of the CLI's paste-echo burst before sending
    Enter as its own keystroke. EOF and the overall deadline both end the
    wait; everything read is appended to `consumed` so the session output
    stays complete.
    """
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        try:
            chunk = process.read_nonblocking(size=65536, timeout=quiet_seconds)
        except pexpect.TIMEOUT:
            return consumed
        except pexpect.EOF:
            return consumed
        consumed = consumed + (chunk or "")
    return consumed


def _drain_pty_stream(process: Any, consumed: str, is_complete: Callable[[str], bool]) -> str:
    """Keep reading PTY output until `is_complete(consumed)` or a deadline.

    `process.expect` returns as soon as its trigger pattern matches, which
    can be mid-escape-sequence or mid-render-frame, so the buffer may hold
    only a prefix of the value being extracted. The CLI animates its spinner
    indefinitely, so there is no reliable quiet gap; completion is judged by
    the caller's predicate, with a hard deadline as backstop.
    """
    deadline = time.monotonic() + _STREAM_DRAIN_DEADLINE_SECONDS
    while not is_complete(consumed) and time.monotonic() < deadline:
        try:
            chunk = process.read_nonblocking(size=65536, timeout=_STREAM_DRAIN_READ_SECONDS)
        except pexpect.TIMEOUT:
            continue
        except pexpect.EOF:
            break
        consumed = consumed + (chunk or "")
    return consumed


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


def _build_restart_with_message_command(names: Sequence[str], message: str) -> list[str]:
    """Build the fused restart argv for previously-RUNNING agents. Pure (see above).

    ``--restart`` stops each agent first; ``--resume-message`` delivers the
    auth-aware continue message through mngr's readiness-aware resume
    machinery after each agent starts.
    """
    return ["mngr", "start", "--restart", "--resume-message", message, *names]


def _build_restart_no_resume_command(names: Sequence[str]) -> list[str]:
    """Build the fused restart argv for previously-WAITING agents. Pure (see above).

    ``--no-resume`` suppresses any message: idle agents come back idle and
    pick up the fresh credentials on their next user message.
    """
    return ["mngr", "start", "--restart", "--no-resume", *names]


class ClaudeAuthService(MutableModel):
    """Stateful entry point for the in-mind Claude auth flows.

    Holds the injected `command_runner` / `pexpect_spawner` dependencies
    and the in-flight setup-token subprocess. One instance is created per
    application and stored on `app.state`; the subprocess held between
    `start_setup_token` and its poll/submit calls rides that instance.
    Tests construct isolated instances with deterministic fakes.
    """

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    command_runner: CommandRunner = _default_command_runner
    pexpect_spawner: PexpectSpawner = _default_pexpect_spawner

    # Only one setup-token flow can be live at a time per instance, which
    # matches the single-mind / single-user deployment model. The lock and
    # the live subprocess are private runtime state, not configuration data.
    _setup_token_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _current_setup_token_record: _AuthFlowSessionRecord | None = PrivateAttr(default=None)
    _current_setup_token_process: Any = PrivateAttr(default=None)
    _current_setup_token_output: str = PrivateAttr(default="")

    # The post-auth agent restart runs on a background thread so the submit
    # endpoints return in seconds (the proxied request path has a 30s
    # ceiling, and a batch restart can take minutes). Single-flight: a new
    # credential change is rejected while a restart is still running.
    _restart_state_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _restart_thread: threading.Thread | None = PrivateAttr(default=None)
    _restart_progress: RestartProgress | None = PrivateAttr(default=None)

    def get_auth_status(self, extra_env: Mapping[str, str] | None = None) -> AuthStatus:
        """Invoke `claude auth status --json` and parse the result.

        Returns `logged_in=False` if the `claude` binary is missing or
        doesn't produce output, rather than raising, since the whole point
        of the modal is to recover from broken auth state.

        The managed env currently in settings.json is overlaid on the
        status subprocess's environment (with `extra_env` layered on top):
        the settings env applies to *new claude processes*, and the status
        subprocess IS one, but the fresh values may not have reached this
        long-lived system-interface process -- the overlay makes the check
        reflect the mind's actual auth source of truth. The settings-derived
        `auth_mode` / `masked_key_suffix` are folded into the returned
        status for the modal's header.
        """
        managed_env = self._read_managed_env_tolerant()
        combined_extra = {**managed_env, **(dict(extra_env) if extra_env else {})}
        runner_env = {**os.environ, **combined_extra} if combined_extra else None
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
            return self._with_derived_mode(AuthStatus(logged_in=False), combined_extra)

        stdout = result.stdout.strip() if isinstance(result.stdout, str) else ""
        if not stdout:
            return self._with_derived_mode(AuthStatus(logged_in=False), combined_extra)
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as e:
            raise ClaudeAuthError(f"claude auth status returned non-JSON output: {stdout!r}") from e
        if not isinstance(payload, dict):
            raise ClaudeAuthError(f"claude auth status returned non-object JSON: {payload!r}")
        return self._with_derived_mode(_parse_status_payload(payload), combined_extra)

    def _read_managed_env_tolerant(self) -> dict[str, str]:
        """Read the managed settings env, tolerating an unset CLAUDE_CONFIG_DIR.

        Status checks must not explode merely because the env var is
        missing (e.g. in a degraded mind) -- they degrade to "no managed
        credentials" and the modal walks the user through recovery.
        """
        try:
            return read_managed_auth_env()
        except ClaudeAuthError as e:
            logger.warning("Cannot read managed auth env: {}", e)
            return {}

    def _with_derived_mode(self, status: AuthStatus, managed_env: Mapping[str, str]) -> AuthStatus:
        progress = self.current_restart_progress()
        # Managed env keys outrank everything claude reads elsewhere, so
        # they define the mode when present. With an empty managed env the
        # mode folds in the credentials-based browser sign-ins: both report
        # authMethod "claude.ai" on the pinned version, discriminated by
        # subscription_type (present only for subscription accounts).
        derived_mode = derive_auth_mode(managed_env)
        if derived_mode is AuthMode.NONE and status.logged_in and status.auth_method == "claude.ai":
            derived_mode = AuthMode.SUBSCRIPTION if status.subscription_type else AuthMode.CONSOLE
        return AuthStatus(
            **{
                **status.model_dump(),
                "auth_mode": derived_mode,
                "masked_key_suffix": masked_credential_suffix(managed_env),
                "workspace_host_id": read_workspace_host_id(),
                "restart_phase": progress.phase.value if progress is not None else None,
                "restart_detail": progress.detail if progress is not None else None,
                "restart_error": progress.error if progress is not None else None,
                "restart_reason": progress.reason.value if progress is not None else None,
            }
        )

    def snapshot_claude_binary_agents(self) -> list[AgentSnapshot]:
        """Return name + state of every claude-binary agent in the local mind.

        Uses `mngr list --format json` and filters to the claude-binary
        types (``claude`` and ``worker``). This excludes the `main`-type
        system-services agent, which has no interactive claude process to
        restart -- and whose restart would tear down every background
        service in the mind.
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
        snapshots: list[AgentSnapshot] = []
        for agent in agents:
            if not isinstance(agent, dict):
                continue
            if agent.get("type") not in CLAUDE_BINARY_AGENT_TYPES:
                continue
            name = agent.get("name")
            if not (isinstance(name, str) and name):
                continue
            state = agent.get("state")
            snapshots.append(AgentSnapshot(name=name, state=state if isinstance(state, str) else ""))
        return snapshots

    def restart_all_claude_agents(self) -> list[str]:
        """Restart every live claude-binary agent via fused `mngr start --restart` calls.

        Snapshots agent states first, then issues one batched call per
        behavior group: previously-RUNNING agents restart with
        `--resume-message` so the auth-aware continue message is delivered
        by mngr's readiness-aware resume machinery, and previously-WAITING
        agents restart with `--no-resume` so they come back idle. STOPPED
        agents are left stopped. Nothing outside the settings env block is
        touched: settings-env credentials do not trip Claude Code's
        API-key challenge (verified empirically on the pinned version),
        and the onboarding dismissals are guaranteed by mngr's claude
        plugin at agent-creation time.

        Returns the list of agent names that were restarted.
        """
        snapshots = self.snapshot_claude_binary_agents()
        running = [s.name for s in snapshots if s.state == _AGENT_STATE_RUNNING]
        waiting = [s.name for s in snapshots if s.state == _AGENT_STATE_WAITING]
        if running:
            self._set_restart_progress(
                RestartPhase.RESTARTING, f"Restarting {len(running)} active agent(s)", None
            )
            logger.info("Restarting previously-RUNNING agents {} via mngr start --restart", running)
            self._run_restart_command(_build_restart_with_message_command(running, RESTART_CONTINUE_MESSAGE))
        if waiting:
            self._set_restart_progress(
                RestartPhase.RESTARTING, f"Restarting {len(waiting)} idle agent(s)", None
            )
            logger.info("Restarting previously-WAITING agents {} via mngr start --restart", waiting)
            self._run_restart_command(_build_restart_no_resume_command(waiting))
        return running + waiting

    def _run_restart_command(self, command: list[str]) -> None:
        result = self.command_runner(command, _MNGR_RESTART_TIMEOUT_SECONDS)
        if result.returncode != 0:
            stderr = result.stderr.strip() if isinstance(result.stderr, str) else ""
            raise ClaudeAuthError(f"{' '.join(command[:3])} failed (exit {result.returncode}): {stderr}")

    def _set_restart_progress(self, phase: RestartPhase, detail: str | None, error: str | None) -> None:
        with self._restart_state_lock:
            reason = self._restart_progress.reason if self._restart_progress is not None else RestartReason.CREDENTIALS_SAVED
            self._restart_progress = RestartProgress(phase=phase, detail=detail, error=error, reason=reason)

    def current_restart_progress(self) -> RestartProgress | None:
        with self._restart_state_lock:
            return self._restart_progress

    def _clear_terminal_restart_progress(self) -> None:
        """Drop restart progress left over from a previous, finished apply.

        The subscription fast path performs no restart, so a stale terminal
        phase (DONE or FAILED) from an earlier credential change must not
        leak into the status it returns -- the frontend routes a "failed"
        phase to the error screen, which would misreport the successful
        sign-in. An apply that is genuinely still running keeps its
        progress (its thread is alive), so the status stays truthful.
        """
        with self._restart_state_lock:
            if self._restart_thread is None or not self._restart_thread.is_alive():
                self._restart_progress = None

    def start_background_apply(
        self,
        managed_env: Mapping[str, str],
        on_complete: Callable[[], object] | None,
        reason: RestartReason,
    ) -> None:
        """Write new managed credentials and restart agents on a background thread.

        The submit endpoints call this and return immediately; the frontend
        follows the apply through the `restart_*` fields on the status
        endpoint. There is deliberately no pre-restart credential probe: a
        bad credential surfaces on the agent's first request, where the
        transcript auth-error detection reopens the modal. `on_complete`
        runs after a successful restart (the welcome-resend check, which
        needs the chat agent back up).
        """
        with self._restart_state_lock:
            if self._restart_thread is not None and self._restart_thread.is_alive():
                raise ClaudeAuthError(
                    "An agent restart from a previous credential change is still in progress; "
                    "wait a moment and try again."
                )
            self._restart_progress = RestartProgress(
                phase=RestartPhase.RESTARTING, detail="Preparing to restart agents", error=None, reason=reason
            )
            thread = threading.Thread(
                target=self._run_apply_in_background,
                args=(dict(managed_env), on_complete),
                name="claude-auth-apply",
                daemon=True,
            )
            self._restart_thread = thread
            thread.start()

    def _run_apply_in_background(self, managed_env: dict[str, str], on_complete: Callable[[], object] | None) -> None:
        # Thread entry point: this is the top-level handler for the apply
        # thread, so any escaping exception is caught, logged, and surfaced
        # to the frontend through the FAILED progress phase instead of
        # dying silently.
        try:
            write_managed_auth_env(managed_env)
            self.restart_all_claude_agents()
            self._set_restart_progress(RestartPhase.FINISHING, "Resuming your agent", None)
            if on_complete is not None:
                on_complete()
            self._set_restart_progress(RestartPhase.DONE, None, None)
        except Exception as e:
            # Deliberately NOT logger.opt(exception=...): loguru's diagnose
            # mode renders frame locals into the log, and this thread's
            # frames hold the raw credential.
            logger.error("Background credential apply failed: {}: {}", type(e).__name__, e)
            self._set_restart_progress(RestartPhase.FAILED, None, str(e))

    def submit_credentials(self, pasted_text: str, on_restart_complete: Callable[[], object] | None) -> AuthStatus:
        """Parse pasted credentials, write the settings env block, start the restart.

        The single chokepoint for the API-key field, the Imbue blob
        textarea, and the subtle direct-token paste: all three arrive as
        env-var-style lines and land in the fully-controlled settings env
        block. All claude-binary agents must be restarted: settings env is
        read at process start, so already-running claudes won't pick up the
        new credentials until their tmux sessions are torn down and
        respawned. The restart runs on a background thread; the returned
        status carries its initial `restart_*` progress fields.
        """
        managed_env = parse_credential_lines(pasted_text)
        self.start_background_apply(managed_env, on_restart_complete, RestartReason.CREDENTIALS_SAVED)
        return self.get_auth_status(extra_env=managed_env)

    def _spawn_auth_flow_and_parse_url(self, args: list[str]) -> tuple[Any, str, str]:
        process = self.pexpect_spawner(
            "claude",
            args,
            _OAUTH_URL_WAIT_SECONDS,
        )
        match_index = process.expect([_OAUTH_URL_REGEX, pexpect.EOF, pexpect.TIMEOUT])
        if match_index != 0:
            _safe_terminate(process)
            _safe_close(process)
            if match_index == 1:
                raise ClaudeAuthError(f"claude {' '.join(args)} exited before printing the OAuth URL")
            raise ClaudeAuthError(f"Timed out waiting for the OAuth URL from claude {' '.join(args)}")
        # The expect trigger can fire mid-render-frame -- e.g. inside the OSC 8
        # hyperlink's opening sequence or on the first width-wrapped row of the
        # visible label -- so the consumed buffer may hold only a prefix of the
        # URL. Drain until a *terminated* hyperlink target is extractable (the
        # normal case, satisfied within the same frame); if the CLI emitted no
        # hyperlink, the deadline expires and the visible label is de-wrapped
        # from everything drained.
        initial_consumed = (process.before or "") + (process.after or "")
        consumed = _drain_pty_stream(
            process,
            initial_consumed,
            lambda buffer: _extract_oauth_url_from_hyperlink(buffer) is not None,
        )
        oauth_url = _extract_oauth_url(consumed)
        if oauth_url is None:
            _safe_terminate(process)
            _safe_close(process)
            raise ClaudeAuthError(
                "OAuth URL matched in the stream but could not be extracted after stripping terminal escape sequences"
            )
        return process, oauth_url, consumed

    def start_setup_token(self) -> AuthFlowStartResult:
        """Spawn `claude setup-token` and return the parsed OAuth URL.

        Replaces any prior in-flight session: only one PTY auth flow can
        be live at a time per instance, which matches the single-mind /
        single-user deployment model. The subprocess then polls Anthropic
        on its own; the frontend drives `poll_setup_token` until the token
        appears (or pastes a code via `submit_setup_token_code` if the CLI
        demands one).
        """
        with self._setup_token_lock:
            self._drop_current_session_locked()
            process, oauth_url, consumed = self._spawn_auth_flow_and_parse_url(["setup-token"])
            record = _AuthFlowSessionRecord(
                session_id=uuid.uuid4().hex, kind=AuthFlowKind.SETUP_TOKEN, provider=None, oauth_url=oauth_url
            )
            self._current_setup_token_record = record
            self._current_setup_token_process = process
            self._current_setup_token_output = consumed
        return AuthFlowStartResult(session_id=record.session_id, oauth_url=record.oauth_url)

    def start_oauth_login(self, provider: OAuthProvider) -> AuthFlowStartResult:
        """Spawn `claude auth login --<provider>` and return the parsed OAuth URL.

        The credentials-based browser sign-ins: `--claudeai` writes a
        subscription credential that running claudes re-read on their next
        API call (no restart when the managed env is empty); `--console`
        stores its key inside `.claude.json`, which claudes cache at
        process start, so it always takes the restart path.
        """
        with self._setup_token_lock:
            self._drop_current_session_locked()
            process, oauth_url, consumed = self._spawn_auth_flow_and_parse_url(
                ["auth", "login", f"--{provider.value}"]
            )
            record = _AuthFlowSessionRecord(
                session_id=uuid.uuid4().hex, kind=AuthFlowKind.OAUTH_LOGIN, provider=provider, oauth_url=oauth_url
            )
            self._current_setup_token_record = record
            self._current_setup_token_process = process
            self._current_setup_token_output = consumed
        return AuthFlowStartResult(session_id=record.session_id, oauth_url=record.oauth_url)

    def _drop_current_session_locked(self) -> None:
        if self._current_setup_token_process is not None:
            _safe_terminate(self._current_setup_token_process)
            _safe_close(self._current_setup_token_process)
        self._current_setup_token_record = None
        self._current_setup_token_process = None
        self._current_setup_token_output = ""

    def _pump_setup_token_output_locked(self, timeout_seconds: float) -> str | None:
        """Read newly available subprocess output; return the token if it appeared.

        Uses a short expect against the token pattern so each poll returns
        promptly. On EOF the accumulated buffer is scanned once more (the
        token and process exit can arrive together); an EOF without a token
        anywhere in the output means the subprocess failed.
        """
        process = self._current_setup_token_process
        try:
            match_index = process.expect(
                [_SETUP_TOKEN_REGEX, _OAUTH_ERROR_REGEX, pexpect.EOF, pexpect.TIMEOUT], timeout=timeout_seconds
            )
        except pexpect.ExceptionPexpect as e:
            raise ClaudeAuthError(f"claude setup-token subprocess failed while waiting for the token: {e}") from e
        self._current_setup_token_output += (process.before or "") + (
            process.after if isinstance(process.after, str) else ""
        )
        if match_index == 1:
            raise ClaudeAuthError(
                "Sign-in was not accepted (OAuth error). The pasted code may be wrong, expired, "
                "or from an earlier sign-in attempt. Please start over."
            )
        if match_index == 0:
            # The trigger fires on the first (possibly width-wrapped) token
            # fragment, but ANY mid-render screen is racy: while the token's
            # first row is drawn, the row under it can still hold the
            # previous frame's content, which is indistinguishable from a
            # terminated one-row value. `claude setup-token` exits right
            # after printing the token, so drain all the way to EOF (the
            # deadline is only a hang backstop) and extract from the final,
            # stable screen.
            self._current_setup_token_output = _drain_pty_stream(
                process,
                self._current_setup_token_output,
                lambda buffer: False,
            )
        token = _extract_setup_token(self._current_setup_token_output)
        if token is not None:
            # Length is safe metadata and the key diagnostic for wrap bugs
            # (a real token is ~108 characters; a screen-width multiple
            # means a truncated extraction).
            logger.info("Extracted setup token (length={})", len(token))
            return token
        if match_index == 2:
            raise ClaudeAuthError("claude setup-token exited without printing a token")
        return None

    def _complete_setup_token_locked(self, token: str, on_restart_complete: Callable[[], object] | None) -> AuthStatus:
        """Hand the minted token to the background apply, drop the session."""
        self._drop_current_session_locked()
        managed_env = {CLAUDE_CODE_OAUTH_TOKEN_ENV_VAR: token}
        self.start_background_apply(managed_env, on_restart_complete, RestartReason.CREDENTIALS_SAVED)
        return self.get_auth_status(extra_env=managed_env)

    def poll_setup_token(
        self, session_id: str, on_restart_complete: Callable[[], object] | None
    ) -> AuthFlowPollResult:
        """Check whether the in-flight setup-token subprocess minted the token yet.

        The browser approval completes the flow CLI-side without any code
        paste (the CLI polls Anthropic), so the frontend just calls this
        periodically. On completion the token is written to the settings
        env block and the background agent restart starts; the returned
        status carries its initial `restart_*` progress fields.
        """
        with self._setup_token_lock:
            record = self._current_setup_token_record
            if record is None or record.session_id != session_id or record.kind is not AuthFlowKind.SETUP_TOKEN:
                raise ClaudeAuthError("No active setup-token session matches the provided session_id")
            try:
                token = self._pump_setup_token_output_locked(_SETUP_TOKEN_POLL_SECONDS)
            except ClaudeAuthError:
                self._drop_current_session_locked()
                raise
            if token is None:
                return AuthFlowPollResult(is_complete=False)
            status = self._complete_setup_token_locked(token, on_restart_complete)
        return AuthFlowPollResult(is_complete=True, status=status)

    def submit_setup_token_code(
        self, session_id: str, code: str, on_restart_complete: Callable[[], object] | None
    ) -> AuthStatus:
        """Send the user's pasted `CODE#STATE` to the live setup-token subprocess.

        The fallback path for flows where the CLI actually prompts for a
        code paste instead of completing via its own polling.
        """
        with self._setup_token_lock:
            record = self._current_setup_token_record
            process = self._current_setup_token_process
            if (
                record is None
                or process is None
                or record.session_id != session_id
                or record.kind is not AuthFlowKind.SETUP_TOKEN
            ):
                raise ClaudeAuthError("No active setup-token session matches the provided session_id")
            self._send_code_locked(process, code)
            try:
                token = self._pump_setup_token_output_locked(_SETUP_TOKEN_CODE_WAIT_SECONDS)
            except ClaudeAuthError:
                self._drop_current_session_locked()
                raise
            if token is None:
                self._drop_current_session_locked()
                raise ClaudeAuthError("Timed out waiting for claude setup-token to print the token after code submit")
            status = self._complete_setup_token_locked(token, on_restart_complete)
        return status

    def _send_code_locked(self, process: Any, code: str) -> None:
        """Type a `CODE#STATE` paste into the live PTY, then a separate Enter.

        Two separate writes: the CLI's paste heuristic swallows a newline
        arriving in the same burst as the code (it becomes field content,
        not a submit). Completion of the burst is observable -- the input
        field echoes the paste as render output -- so Enter is sent once
        the echo stream goes quiet (with a deadline backstop) rather than
        after a fixed sleep.
        """
        try:
            process.send(code)
            self._current_setup_token_output = _drain_pty_stream_until_quiet(
                process,
                self._current_setup_token_output,
                _CODE_ECHO_QUIET_SECONDS,
                _CODE_ECHO_DEADLINE_SECONDS,
            )
            process.send("\r")
        except pexpect.ExceptionPexpect as e:
            self._drop_current_session_locked()
            raise ClaudeAuthError(f"auth subprocess failed sending code: {e}") from e

    def _pump_oauth_login_output_locked(self, timeout_seconds: float) -> bool:
        """Read `claude auth login` output; return True once it reports success.

        The CLI prints a plain `Login successful.` and exits 0 (or `Login
        failed: ...` and exits 1), so completion detection needs no screen
        replay. A transient `OAuth error` (rejected/stale code) fails fast
        with the same copy as the setup-token flow.
        """
        process = self._current_setup_token_process
        try:
            match_index = process.expect(
                [_LOGIN_SUCCESS_REGEX, _LOGIN_FAILED_LINE_REGEX, _OAUTH_ERROR_REGEX, pexpect.EOF, pexpect.TIMEOUT],
                timeout=timeout_seconds,
            )
        except pexpect.ExceptionPexpect as e:
            raise ClaudeAuthError(f"claude auth login subprocess failed while waiting for completion: {e}") from e
        self._current_setup_token_output += (process.before or "") + (
            process.after if isinstance(process.after, str) else ""
        )
        if match_index == 2:
            raise ClaudeAuthError(
                "Sign-in was not accepted (OAuth error). The pasted code may be wrong, expired, "
                "or from an earlier sign-in attempt. Please start over."
            )
        if match_index == 0:
            # Drain the goodbye output so the process reaps cleanly.
            self._current_setup_token_output = _drain_pty_stream(process, self._current_setup_token_output, lambda buffer: False)
            return True
        if match_index in (1, 3):
            # Failure line matched, or EOF: the buffer decides (success and
            # exit can arrive in one read, so EOF does not imply failure).
            self._current_setup_token_output = _drain_pty_stream(process, self._current_setup_token_output, lambda buffer: False)
            if _LOGIN_SUCCESS_REGEX.search(self._current_setup_token_output):
                return True
            failed_match = _LOGIN_FAILED_LINE_REGEX.search(self._current_setup_token_output)
            failure_detail = failed_match.group(1).strip() if failed_match is not None else ""
            raise ClaudeAuthError(
                f"Sign-in did not complete: {failure_detail or 'claude auth login exited without logging in'}"
            )
        return False

    def _complete_oauth_login_locked(
        self, provider: OAuthProvider, on_restart_complete: Callable[[], object] | None
    ) -> AuthStatus:
        """Apply a finished browser sign-in: fast path or switch-restart.

        The credential is already stored by the CLI itself. With an empty
        managed env and the subscription provider, nothing else is needed:
        running claudes re-read the credential on their next API call, so
        the welcome-resend hook runs inline and no restart happens. When
        managed keys are active they would keep outranking the fresh
        credential, so they are cleared and the agents restarted; Console
        always restarts (its key lives in `.claude.json`, cached at claude
        process start).
        """
        self._drop_current_session_locked()
        managed_env = self._read_managed_env_tolerant()
        if provider is OAuthProvider.CLAUDEAI and not managed_env:
            self._clear_terminal_restart_progress()
            status = self.get_auth_status()
            if on_restart_complete is not None:
                on_restart_complete()
            return status
        reason = (
            RestartReason.CONSOLE_SWITCH if provider is OAuthProvider.CONSOLE else RestartReason.SUBSCRIPTION_SWITCH
        )
        self.start_background_apply({}, on_restart_complete, reason)
        return self.get_auth_status()

    def poll_oauth_login(
        self, session_id: str, on_restart_complete: Callable[[], object] | None
    ) -> AuthFlowPollResult:
        """Check whether the in-flight `claude auth login` finished on its own.

        Like setup-token, the CLI polls Anthropic itself after the browser
        approval, so the frontend just calls this periodically; the pasted
        code is the always-available path.
        """
        with self._setup_token_lock:
            record = self._current_setup_token_record
            if record is None or record.session_id != session_id or record.kind is not AuthFlowKind.OAUTH_LOGIN:
                raise ClaudeAuthError("No active browser sign-in session matches the provided session_id")
            provider = record.provider
            if provider is None:
                raise ClaudeAuthError("Browser sign-in session is missing its provider")
            try:
                is_complete = self._pump_oauth_login_output_locked(_SETUP_TOKEN_POLL_SECONDS)
            except ClaudeAuthError:
                self._drop_current_session_locked()
                raise
            if not is_complete:
                return AuthFlowPollResult(is_complete=False)
            status = self._complete_oauth_login_locked(provider, on_restart_complete)
        return AuthFlowPollResult(is_complete=True, status=status)

    def submit_oauth_login_code(
        self, session_id: str, code: str, on_restart_complete: Callable[[], object] | None
    ) -> AuthStatus:
        """Send the user's pasted `CODE#STATE` to the live `claude auth login`."""
        with self._setup_token_lock:
            record = self._current_setup_token_record
            process = self._current_setup_token_process
            if (
                record is None
                or process is None
                or record.session_id != session_id
                or record.kind is not AuthFlowKind.OAUTH_LOGIN
            ):
                raise ClaudeAuthError("No active browser sign-in session matches the provided session_id")
            provider = record.provider
            if provider is None:
                raise ClaudeAuthError("Browser sign-in session is missing its provider")
            self._send_code_locked(process, code)
            try:
                is_complete = self._pump_oauth_login_output_locked(_SETUP_TOKEN_CODE_WAIT_SECONDS)
            except ClaudeAuthError:
                self._drop_current_session_locked()
                raise
            if not is_complete:
                self._drop_current_session_locked()
                raise ClaudeAuthError("Timed out waiting for claude auth login to complete after code submit")
            status = self._complete_oauth_login_locked(provider, on_restart_complete)
        return status

    def abort_auth_flow(self) -> None:
        """Drop any in-flight PTY auth session (e.g. user closed the modal)."""
        with self._setup_token_lock:
            self._drop_current_session_locked()
