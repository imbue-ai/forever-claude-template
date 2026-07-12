"""Shared configuration and constants for the opt-in GitHub sync.

GitHub sync is disabled by default. The github-sync skill enables it by
writing github_sync.toml at the repo root (the presence of that file is the
"sync is configured" marker), wiring git up to push through the latchkey
gateway, and adding the [program:github-sync] supervisord service.
"""

import os
import tomllib
from pathlib import Path

# All relative paths assume cwd = repo root (/mngr/code), matching
# supervisord's `directory=` and the other template services.
RUNTIME_DIR = Path("runtime")
CONFIG_PATH = Path("github_sync.toml")

# The stable branch that runtime/ state is synced to. The sync repo is a
# dedicated private repo per workspace, so no per-agent namespacing is needed.
SYNC_BRANCH = "runtime-sync"

GITHUB_URL_PREFIX = "https://github.com/"

# Env vars injected by mngr_latchkey's prepare_agent_latchkey for every agent.
# The secondary gateway (remote VPS hosts only) keeps working when the user's
# own machine -- which runs the primary gateway -- is offline.
ENV_GATEWAY = "LATCHKEY_GATEWAY"
ENV_GATEWAY_SECONDARY = "LATCHKEY_GATEWAY_SECONDARY"
ENV_GATEWAY_PASSWORD = "LATCHKEY_GATEWAY_PASSWORD"
ENV_GATEWAY_PERMISSIONS_OVERRIDE = "LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE"


class GithubSyncError(Exception):
    """Base error for the github_sync library."""


class GithubSyncConfigError(GithubSyncError, ValueError):
    """Raised when github_sync.toml exists but is not a valid sync config."""


def load_repo_url() -> str | None:
    """Return the sync repo URL from github_sync.toml, or None when sync is not configured.

    Raises GithubSyncConfigError when the file exists but is malformed: a
    skill-authored config problem should be loud, not silently ignored.
    """
    if not CONFIG_PATH.exists():
        return None
    try:
        raw_text = CONFIG_PATH.read_text()
    except OSError as e:
        raise GithubSyncConfigError(f"Cannot read {CONFIG_PATH}: {e}") from e
    try:
        data = tomllib.loads(raw_text)
    except tomllib.TOMLDecodeError as e:
        raise GithubSyncConfigError(f"Cannot parse {CONFIG_PATH}: {e}") from e
    repo_url = data.get("repo_url")
    if not isinstance(repo_url, str) or not repo_url.startswith(GITHUB_URL_PREFIX):
        raise GithubSyncConfigError(
            f"{CONFIG_PATH} must set repo_url to an "
            f"{GITHUB_URL_PREFIX}<owner>/<repo> URL, got {repo_url!r}"
        )
    return repo_url.removesuffix(".git").rstrip("/")


def parse_owner_and_name(repo_url: str) -> tuple[str, str]:
    """Split a GitHub repo URL into (owner, name).

    Raises GithubSyncConfigError when the URL is not of the form
    https://github.com/<owner>/<repo>.
    """
    path = repo_url.removeprefix(GITHUB_URL_PREFIX).removesuffix(".git")
    parts = [part for part in path.split("/") if part]
    if len(parts) != 2:
        raise GithubSyncConfigError(
            f"Repo URL must be {GITHUB_URL_PREFIX}<owner>/<repo>, got {repo_url!r}"
        )
    return parts[0], parts[1]


def get_gateway_url() -> str | None:
    """The primary latchkey gateway URL (no trailing slash), or None if unset."""
    value = os.environ.get(ENV_GATEWAY, "").rstrip("/")
    return value or None


def get_secondary_gateway_url() -> str | None:
    """The per-VPS backup gateway URL (no trailing slash), or None if unset."""
    value = os.environ.get(ENV_GATEWAY_SECONDARY, "").rstrip("/")
    return value or None


def get_gateway_password() -> str | None:
    value = os.environ.get(ENV_GATEWAY_PASSWORD, "")
    return value or None


def get_gateway_permissions_override() -> str | None:
    value = os.environ.get(ENV_GATEWAY_PERMISSIONS_OVERRIDE, "")
    return value or None


def proxied_url(gateway_url: str, target_url: str) -> str:
    """The gateway's git smart-HTTP proxy URL for a GitHub target URL."""
    return f"{gateway_url}/gateway/{target_url}"
