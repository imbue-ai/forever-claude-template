"""Tests for the scripts/git_hooks/post-commit auto-push hook.

The hook is the third piece of the opt-in GitHub sync (see the README): it
auto-pushes the committed branch of any checkout, but only when sync is
configured, and never while the github-sync service has halted pushes because
the sync repo is not confirmed private. The halt is the security-critical
behavior, so it gets a direct regression test (an earlier version used
jq's `//` operator, which turned an explicit ``"is_push_allowed": false``
into ``true`` and silently disabled the halt).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from github_sync.testing import init_repo_with_origin

_HOOK_DIR = Path(__file__).parents[4] / "scripts" / "git_hooks"

_PUSH_WAIT_SECONDS = 20.0


def _commit_with_hook(
    repo: Path, tmp_path: Path, is_push_allowed: bool | None, is_configured: bool = True
) -> Path:
    """Make a hook-firing commit in `repo`; returns the hook log path.

    `is_push_allowed=None` means no status file (service has not reported yet).
    """
    config_path = tmp_path / "github_sync.toml"
    if is_configured:
        config_path.write_text(
            'repo_url = "https://github.com/some-user/my-workspace"\n'
        )
    status_path = tmp_path / "github-sync-status.json"
    if is_push_allowed is not None:
        status_path.write_text(json.dumps({"is_push_allowed": is_push_allowed}))
    log_path = tmp_path / "post-commit-push.log"

    subprocess.run(
        ["git", "-C", str(repo), "config", "core.hooksPath", str(_HOOK_DIR)],
        check=True,
        capture_output=True,
    )
    (repo / "change.txt").write_text("change\n")
    subprocess.run(
        ["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True
    )
    hook_env = {
        **os.environ,
        # Never let a push attempt block on a TTY credential prompt.
        "GIT_TERMINAL_PROMPT": "0",
        "GITHUB_SYNC_CONFIG_FILE": str(config_path),
        "GITHUB_SYNC_STATUS_FILE": str(status_path),
        "GITHUB_SYNC_HOOK_LOG_FILE": str(log_path),
    }
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "change"],
        check=True,
        capture_output=True,
        env=hook_env,
    )
    return log_path


def _origin_has_branch(origin: Path, branch: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(origin), "rev-parse", "--verify", branch],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def test_hook_pushes_committed_branch_when_push_allowed(
    tmp_path: Path, isolated_git_and_gateway_env: Path
) -> None:
    """With sync configured and the repo confirmed private, a commit's branch
    lands on origin (the push runs in the background, so poll for it)."""
    main, origin = init_repo_with_origin(tmp_path)

    _commit_with_hook(main, tmp_path, is_push_allowed=True)

    deadline = time.monotonic() + _PUSH_WAIT_SECONDS
    while not _origin_has_branch(origin, "main"):
        assert time.monotonic() < deadline, "hook never pushed the branch"
        time.sleep(0.1)


def test_hook_skips_push_during_visibility_halt(
    tmp_path: Path, isolated_git_and_gateway_env: Path
) -> None:
    """Regression: an explicit `"is_push_allowed": false` must block the push
    (jq's `//` operator once turned it into `true`). The halt path is fully
    synchronous -- no background push job is spawned -- so asserting right
    after the commit is race-free."""
    if shutil.which("jq") is None:
        pytest.skip("jq is not installed; the hook's halt check requires it")
    main, origin = init_repo_with_origin(tmp_path)

    log_path = _commit_with_hook(main, tmp_path, is_push_allowed=False)

    assert not _origin_has_branch(origin, "main")
    assert "skipping push of main: sync repo not confirmed private" in (
        log_path.read_text()
    )


def test_hook_does_nothing_when_sync_not_configured(
    tmp_path: Path, isolated_git_and_gateway_env: Path
) -> None:
    """Without github_sync.toml the hook must exit before doing anything."""
    main, origin = init_repo_with_origin(tmp_path)

    log_path = _commit_with_hook(
        main, tmp_path, is_push_allowed=None, is_configured=False
    )

    assert not _origin_has_branch(origin, "main")
    assert not log_path.exists()
