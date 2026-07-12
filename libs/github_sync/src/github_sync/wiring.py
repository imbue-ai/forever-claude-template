"""Global git config wiring that routes GitHub traffic through the latchkey gateway.

Applied by the github-sync skill at enable time and re-applied by the service
when a push fails (self-healing a gateway URL that changed across container
restarts). With the wiring in place, plain `git push` / `git fetch` against any
https://github.com/... remote is transparently rewritten to the gateway's git
proxy and authenticated with the gateway headers -- for every checkout in the
container (main repo, worker worktrees, the runtime/ worktree). The GitHub
credential itself is injected server-side by the gateway; no token ever enters
the container.

The wiring also points core.hooksPath at the repo's git_hooks so the
post-commit auto-push hook applies everywhere.
"""

import subprocess

from loguru import logger

from github_sync.config import (
    GITHUB_URL_PREFIX,
    get_gateway_password,
    get_gateway_permissions_override,
    get_gateway_url,
    proxied_url,
)

HOOKS_PATH = "/mngr/code/scripts/git_hooks"
PASSWORD_HEADER = "X-Latchkey-Gateway-Password"
PERMISSIONS_OVERRIDE_HEADER = "X-Latchkey-Gateway-Permissions-Override"


def _git_config(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a `git config --global` command, never raising."""
    return subprocess.run(
        ["git", "config", "--global", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _list_global_config(key_regexp: str) -> list[tuple[str, str]]:
    """List (key, value) pairs of global git config entries matching a key regexp."""
    result = _git_config("--get-regexp", key_regexp)
    if result.returncode != 0:
        # rc=1 simply means no matching entries.
        return []
    entries: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        key, _, value = line.partition(" ")
        if key:
            entries.append((key, value))
    return entries


def _remove_gateway_entries(kept_gateway_url: str | None) -> None:
    """Remove latchkey-gateway git config entries for every other gateway URL.

    With `kept_gateway_url=None` this removes all of them (the disable path).
    Gateway URLs embed a reverse-tunneled local port that can change across
    container restarts, so re-wiring must clean up entries pointing at a stale
    port -- two insteadOf rewrites for the same prefix would be ambiguous.
    """
    kept_insteadof_key = (
        f"url.{proxied_url(kept_gateway_url, GITHUB_URL_PREFIX)}.insteadof"
        if kept_gateway_url is not None
        else None
    )
    for key, value in _list_global_config(r"url\..*\.insteadof"):
        is_gateway_rewrite = value == GITHUB_URL_PREFIX and "/gateway/" in key
        if is_gateway_rewrite and key.lower() != (kept_insteadof_key or "").lower():
            _git_config("--unset-all", key)

    kept_header_key = (
        f"http.{kept_gateway_url}/.extraheader" if kept_gateway_url is not None else None
    )
    for key, value in _list_global_config(r"http\..*\.extraheader"):
        is_gateway_header = value.startswith(
            (PASSWORD_HEADER, PERMISSIONS_OVERRIDE_HEADER)
        )
        if is_gateway_header and key.lower() != (kept_header_key or "").lower():
            _git_config("--unset-all", key)


def apply_git_wiring() -> bool:
    """Install the gateway rewrite, auth headers, and hooks path in global git config.

    Idempotent; safe to re-run on every service self-heal. Returns False (with
    a logged warning) when the latchkey gateway env vars are not all present.
    """
    gateway_url = get_gateway_url()
    password = get_gateway_password()
    permissions_override = get_gateway_permissions_override()
    if not gateway_url or not password or not permissions_override:
        logger.warning(
            "Latchkey gateway env is incomplete (need LATCHKEY_GATEWAY, "
            "LATCHKEY_GATEWAY_PASSWORD, LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE); "
            "cannot wire git for GitHub sync"
        )
        return False

    _remove_gateway_entries(gateway_url)

    header_key = f"http.{gateway_url}/.extraHeader"
    config_steps = (
        (
            "--replace-all",
            f"url.{proxied_url(gateway_url, GITHUB_URL_PREFIX)}.insteadOf",
            GITHUB_URL_PREFIX,
        ),
        ("--replace-all", header_key, f"{PASSWORD_HEADER}: {password}"),
        ("--add", header_key, f"{PERMISSIONS_OVERRIDE_HEADER}: {permissions_override}"),
        ("--replace-all", "core.hooksPath", HOOKS_PATH),
    )
    for argv in config_steps:
        result = _git_config(*argv)
        if result.returncode != 0:
            logger.warning(
                "git config --global {} failed (rc={}): {}",
                " ".join(argv),
                result.returncode,
                result.stderr.strip(),
            )
            return False
    logger.debug("Wired git GitHub access through gateway {}", gateway_url)
    return True


def remove_git_wiring() -> None:
    """Remove every gateway git config entry and the hooks path (disable path)."""
    _remove_gateway_entries(None)
    _git_config("--unset-all", "core.hooksPath")
    logger.info("Removed GitHub sync git wiring from global git config")
