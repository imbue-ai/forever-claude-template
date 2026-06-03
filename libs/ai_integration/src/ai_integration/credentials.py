"""Credential resolution and ``claude -p`` child-environment construction.

Two responsibilities:

1. Decide whether the direct API or the ``claude -p`` fallback is usable, and
   fail *loudly* when neither is -- so a service surfaces a clear error rather
   than an opaque auth failure from deep inside ``claude``.
2. Build the environment for a spawned ``claude -p`` so it authenticates and does
   not trip the mngr session-hook bug: ``MAIN_CLAUDE_SESSION_ID`` must be unset
   in the child, or every mngr stop/readiness hook (all guarded on that var)
   treats the child as the managed main session and engages its machinery.
"""

import json
import os
from collections.abc import Mapping
from pathlib import Path

from ai_integration.errors import CredentialsUnavailableError

MAIN_CLAUDE_SESSION_ID = "MAIN_CLAUDE_SESSION_ID"

# Additional mngr identity vars that mngr's own subagent proxy strips when
# spawning a child. Not required to fix the session-hook bug (the hooks are all
# guarded on MAIN_CLAUDE_SESSION_ID), but available as defense-in-depth.
_MNGR_AGENT_VARS = ("MNGR_AGENT_STATE_DIR", "MNGR_AGENT_NAME", "MNGR_HOST_DIR")


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def get_api_key(env: Mapping[str, str] | None = None) -> str | None:
    """Return ``ANTHROPIC_API_KEY`` from the environment, or ``None`` if unset/empty."""
    return _env(env).get("ANTHROPIC_API_KEY") or None


def _claude_config_dirs(env: Mapping[str, str]) -> list[Path]:
    dirs: list[Path] = []
    config_dir = env.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        dirs.append(Path(config_dir))
    home = env.get("HOME")
    home_path = Path(home) if home else Path.home()
    dirs.append(home_path / ".claude")
    return dirs


def has_claude_cli_credentials(env: Mapping[str, str] | None = None) -> bool:
    """Whether ``claude -p`` has a credential path (OAuth file or primaryApiKey).

    Checks the resolved ``CLAUDE_CONFIG_DIR`` (and ``~/.claude``) for a
    ``.credentials.json`` (OAuth), and ``~/.claude.json`` for a ``primaryApiKey``.
    An ``ANTHROPIC_API_KEY`` in the env would also work but is checked separately
    by ``get_api_key`` / ``has_resolvable_credentials``.
    """
    resolved = _env(env)
    for config_dir in _claude_config_dirs(resolved):
        if (config_dir / ".credentials.json").is_file():
            return True
    home = resolved.get("HOME")
    home_path = Path(home) if home else Path.home()
    claude_json = home_path / ".claude.json"
    if claude_json.is_file():
        try:
            data = json.loads(claude_json.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if isinstance(data, dict) and data.get("primaryApiKey"):
            return True
    return False


def has_resolvable_credentials(env: Mapping[str, str] | None = None) -> bool:
    """Whether *any* usable credential path exists (direct API key or claude -p)."""
    return get_api_key(env) is not None or has_claude_cli_credentials(env)


def require_credentials(env: Mapping[str, str] | None = None) -> None:
    """Raise ``CredentialsUnavailableError`` if no credential path is resolvable."""
    if not has_resolvable_credentials(env):
        raise CredentialsUnavailableError(
            "No Claude credentials available: set ANTHROPIC_API_KEY for direct-API "
            "billing, or ensure CLAUDE_CONFIG_DIR / ~/.claude has credentials so "
            "`claude -p` can authenticate."
        )


def build_claude_cli_env(
    env: Mapping[str, str] | None = None,
    strip_mngr_agent_vars: bool = False,
) -> dict[str, str]:
    """Build the child environment for a spawned ``claude -p``.

    Always unsets ``MAIN_CLAUDE_SESSION_ID`` (required: an inherited value makes
    the child look like the managed main session and engages mngr's session-hook
    machinery). When ``strip_mngr_agent_vars`` is set, also drops the mngr
    identity vars as defense-in-depth (mirrors mngr's subagent proxy).
    """
    child = dict(_env(env))
    child.pop(MAIN_CLAUDE_SESSION_ID, None)
    if strip_mngr_agent_vars:
        for var in _MNGR_AGENT_VARS:
            child.pop(var, None)
    return child
