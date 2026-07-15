"""Sync-repo visibility checking through latchkey.

Public repos must never be synced to: agents might push secrets or other
sensitive data without thinking about it. The skill verifies the repo is
private at enable time, and the service re-checks periodically: pushes are
held until visibility is first confirmed private and halted whenever the
repo is confirmed public, while a re-check that fails outright keeps the
last confirmed answer (see runner._refresh_visibility for that policy).
"""

import json
import os
import subprocess

from loguru import logger

from github_sync.config import (
    ENV_GATEWAY,
    ENV_GATEWAY_PERMISSIONS_OVERRIDE,
    get_secondary_gateway_url,
    parse_owner_and_name,
)

VISIBILITY_PRIVATE = "private"
VISIBILITY_PUBLIC = "public"
VISIBILITY_UNKNOWN = "unknown"

_LATCHKEY_CURL_TIMEOUT_SECONDS = 60


def parse_visibility_response(body: str) -> str:
    """Map a GitHub `GET /repos/<owner>/<repo>` response body to a visibility.

    Anything that is not an explicit `"private": true/false` (error bodies,
    truncated output, a 404 for a deleted repo) is UNKNOWN, which callers
    treat as push-blocking.
    """
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return VISIBILITY_UNKNOWN
    if not isinstance(data, dict):
        return VISIBILITY_UNKNOWN
    is_private = data.get("private")
    if is_private is True:
        return VISIBILITY_PRIVATE
    elif is_private is False:
        return VISIBILITY_PUBLIC
    else:
        return VISIBILITY_UNKNOWN


def _latchkey_curl_env(is_secondary: bool) -> dict[str, str] | None:
    """Env for a `latchkey curl` call; None means inherit (primary gateway)."""
    if not is_secondary:
        return None
    secondary_url = get_secondary_gateway_url()
    if secondary_url is None:
        return None
    # Per the latchkey skill: the secondary gateway takes no permissions
    # override, so it must be cleared alongside the gateway swap.
    return {
        **os.environ,
        ENV_GATEWAY: secondary_url,
        ENV_GATEWAY_PERMISSIONS_OVERRIDE: "",
    }


def check_repo_visibility(repo_url: str) -> str:
    """Ask GitHub (via latchkey) whether the sync repo is private.

    Tries the primary gateway first, then the secondary (per-VPS) gateway so
    the check keeps working when the user's machine is offline. Returns
    UNKNOWN when neither gateway produces a parseable answer.
    """
    owner, name = parse_owner_and_name(repo_url)
    api_url = f"https://api.github.com/repos/{owner}/{name}"
    gateway_attempts = [False]
    if get_secondary_gateway_url() is not None:
        gateway_attempts.append(True)
    for is_secondary in gateway_attempts:
        # latchkey curl injects the GitHub credential server-side; -s keeps
        # stdout parseable.
        try:
            result = subprocess.run(
                ["latchkey", "curl", "-s", api_url],
                capture_output=True,
                text=True,
                check=False,
                env=_latchkey_curl_env(is_secondary),
                timeout=_LATCHKEY_CURL_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.debug("latchkey curl failed (secondary={}): {}", is_secondary, e)
            continue
        if result.returncode != 0:
            logger.debug(
                "latchkey curl exited {} (secondary={}): {}",
                result.returncode,
                is_secondary,
                result.stderr.strip(),
            )
            continue
        visibility = parse_visibility_response(result.stdout)
        if visibility != VISIBILITY_UNKNOWN:
            return visibility
    return VISIBILITY_UNKNOWN
