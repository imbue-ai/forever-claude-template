"""GitHub sync service loop.

Polls runtime/ every TICK_INTERVAL_SECONDS; if there are uncommitted changes,
makes a sync commit on the runtime-sync branch checked out at runtime/ and
pushes it to origin through the latchkey gateway. Periodically re-verifies
that the sync repo is still private and halts pushes when it is public or its
visibility cannot be established.

The service only exists when the github-sync skill has enabled sync (it adds
the [program:github-sync] block to supervisord.conf). The skill normally also
creates the runtime/ worktree; if it is missing (e.g. a workspace recreated
from a previously-synced repo), each tick retries the worktree init, which
restores runtime/ from origin once the latchkey GitHub permissions have been
re-granted.
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from github_sync.config import (
    SYNC_BRANCH,
    GithubSyncConfigError,
    get_gateway_password,
    get_secondary_gateway_url,
    load_repo_url,
    proxied_url,
)
from github_sync.visibility import (
    VISIBILITY_PRIVATE,
    VISIBILITY_PUBLIC,
    VISIBILITY_UNKNOWN,
    check_repo_visibility,
)
from github_sync.wiring import PASSWORD_HEADER, apply_git_wiring
from github_sync.worktree import (
    git_runtime,
    init_runtime_worktree,
    is_runtime_worktree,
)

TICK_INTERVAL_SECONDS = 60
# How long a confirmed visibility answer stays fresh before it is re-checked.
# Failed checks are retried every tick instead.
VISIBILITY_CHECK_INTERVAL_SECONDS = 900
LOG_FILE = Path("/tmp/github-sync.log")
# Machine-readable status mirror, read by the post-commit hook (to respect a
# visibility halt) and by the github-sync skill's status report. Lives in /tmp
# deliberately: it is per-boot state, not something to sync. The default path
# is what scripts/git_hooks/post-commit reads.
DEFAULT_STATUS_FILE = Path("/tmp/github-sync-status.json")

# Minimum age before an index.lock is treated as stale and removed. A real git
# operation on the small runtime/ tree holds the lock for well under a second,
# so a lock older than this cannot belong to a live operation. Set to the tick
# interval so a lock skipped as possibly-live on one tick is guaranteed old
# enough to clear on the next.
STALE_LOCK_MIN_AGE_SECONDS = TICK_INTERVAL_SECONDS


class _SyncState:
    """Mutable sync status carried across ticks and mirrored to the status file."""

    def __init__(self) -> None:
        self.repo_url: str | None = None
        self.visibility: str = VISIBILITY_UNKNOWN
        self.visibility_checked_at: datetime | None = None
        self.last_push_ok: bool | None = None
        self.last_push_at: datetime | None = None
        self.last_error: str | None = None

    @property
    def is_push_allowed(self) -> bool:
        return self.visibility == VISIBILITY_PRIVATE


def status_file_path() -> Path:
    """The status-mirror path; overridable via env so tests stay isolated."""
    override = os.environ.get("GITHUB_SYNC_STATUS_FILE", "")
    return Path(override) if override else DEFAULT_STATUS_FILE


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso_or_none(moment: datetime | None) -> str | None:
    """ISO-8601 with a trailing Z (e.g. 2026-05-06T17:42:13Z), or None."""
    if moment is None:
        return None
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _clear_stale_index_lock() -> None:
    """Remove a stale ``index.lock`` from the runtime worktree, if present.

    github-sync is the only writer of this worktree's git index and its ticks
    run strictly sequentially, so a lock here is normally stale -- left behind
    by a previous tick's git process that was killed before it could release
    the lock (whenever something kills the process mid-commit). Git never
    clears such a lock itself, so without this every subsequent ``git add``
    fails identically and syncing stops forever.

    The removal is deliberately *not* unconditional: to stay safe even if the
    single-writer assumption is ever violated (a concurrent git process in the
    worktree), the lock is only removed once it is older than
    ``STALE_LOCK_MIN_AGE_SECONDS``. A live git operation on the small runtime/
    tree holds the lock for well under a second, so an older lock cannot be
    live; a genuinely in-progress operation is left untouched and reconsidered
    on the next tick.

    The lock path is resolved via ``--absolute-git-dir`` so it is correct
    whether runtime/ is a normal repo or (as in production) a linked worktree
    with a per-worktree git dir.
    """
    git_dir_result = git_runtime("rev-parse", "--absolute-git-dir")
    if git_dir_result.returncode != 0:
        # runtime/ is not a git repo yet; nothing to clear.
        return
    lock_path = Path(git_dir_result.stdout.strip()) / "index.lock"
    try:
        lock_age_seconds = time.time() - lock_path.stat().st_mtime
    except OSError:
        # No lock present (the common case), or it vanished underneath us.
        return
    if lock_age_seconds < STALE_LOCK_MIN_AGE_SECONDS:
        logger.warning(
            "git index.lock at {} is only {:.0f}s old; leaving it in case a "
            "git operation is in progress (will reconsider next tick)",
            lock_path,
            lock_age_seconds,
        )
        return
    logger.warning(
        "Removing stale git index lock at {} ({:.0f}s old)",
        lock_path,
        lock_age_seconds,
    )
    try:
        lock_path.unlink()
    except OSError as e:
        logger.warning("Failed to remove stale index lock at {}: {}", lock_path, e)


def _has_uncommitted_changes() -> bool:
    """Return True if runtime/ has anything to commit."""
    result = git_runtime("status", "--porcelain")
    if result.returncode != 0:
        logger.warning(
            "git status failed (rc={}): {}", result.returncode, result.stderr.strip()
        )
        return False
    return bool(result.stdout.strip())


def _commit_runtime_changes() -> None:
    """Stage and commit anything new in runtime/ (no-op when clean)."""
    add_result = git_runtime("add", "-A")
    if add_result.returncode != 0:
        logger.warning(
            "git add failed (rc={}): {}",
            add_result.returncode,
            add_result.stderr.strip(),
        )
        return
    if _has_uncommitted_changes():
        commit_result = git_runtime(
            "commit", "-m", f"runtime sync: {_iso_or_none(_now_utc())}"
        )
        if commit_result.returncode != 0:
            logger.warning(
                "git commit failed (rc={}): {}",
                commit_result.returncode,
                commit_result.stderr.strip(),
            )


def _refresh_visibility(state: _SyncState, repo_url: str) -> None:
    """Re-check repo visibility when the last confirmed answer has gone stale.

    A completed check updates the state; a failed check (UNKNOWN result)
    leaves the previous answer in place and is retried next tick. Transitions
    are logged loudly since a repo flipping public is a security condition.
    """
    if state.visibility_checked_at is not None and state.visibility != VISIBILITY_UNKNOWN:
        age_seconds = (_now_utc() - state.visibility_checked_at).total_seconds()
        if age_seconds < VISIBILITY_CHECK_INTERVAL_SECONDS:
            return
    visibility = check_repo_visibility(repo_url)
    if visibility == VISIBILITY_UNKNOWN:
        logger.debug("Could not check visibility of {}; will retry", repo_url)
        return
    if visibility != state.visibility:
        if visibility == VISIBILITY_PRIVATE:
            logger.info("Sync repo {} confirmed private; pushes enabled", repo_url)
        else:
            logger.error(
                "Sync repo {} is PUBLIC; halting all sync pushes until it is "
                "made private again",
                repo_url,
            )
    state.visibility = visibility
    state.visibility_checked_at = _now_utc()


def _push_via_secondary_gateway(repo_url: str) -> bool:
    """Push runtime-sync through the per-VPS backup gateway, if one exists.

    Used when the primary gateway (on the user's machine) is unreachable. The
    secondary takes only the gateway password header -- no permissions
    override -- per the latchkey contract. The proxied URL is passed
    explicitly so the global insteadOf rewrite (which targets the primary
    gateway) does not apply.
    """
    secondary_url = get_secondary_gateway_url()
    password = get_gateway_password()
    if secondary_url is None or password is None:
        return False
    push_url = proxied_url(secondary_url, f"{repo_url}.git")
    result = git_runtime(
        "-c",
        f"http.extraHeader={PASSWORD_HEADER}: {password}",
        "push",
        push_url,
        f"{SYNC_BRANCH}:{SYNC_BRANCH}",
    )
    if result.returncode != 0:
        logger.debug(
            "secondary-gateway push failed (rc={}): {}",
            result.returncode,
            result.stderr.strip(),
        )
    return result.returncode == 0


def _push_runtime(state: _SyncState, repo_url: str) -> None:
    """Push the runtime-sync branch, self-healing wiring drift along the way.

    Always attempts a push (even on a clean tick) so commits whose push failed
    earlier still get shipped. The ladder: plain push; re-apply gateway wiring
    (the gateway URL embeds a port that can change across restarts) and retry;
    set-upstream (heals a lost upstream); secondary gateway.
    """
    push_result = git_runtime("push")
    if push_result.returncode != 0:
        apply_git_wiring()
        push_result = git_runtime("push")
    if push_result.returncode != 0:
        push_result = git_runtime("push", "--set-upstream", "origin", SYNC_BRANCH)
    is_pushed = push_result.returncode == 0
    if not is_pushed:
        is_pushed = _push_via_secondary_gateway(repo_url)
    state.last_push_ok = is_pushed
    state.last_push_at = _now_utc()
    if is_pushed:
        state.last_error = None
    else:
        state.last_error = push_result.stderr.strip()
        logger.warning(
            "git push failed (rc={}): {}",
            push_result.returncode,
            push_result.stderr.strip(),
        )


def _write_status(state: _SyncState) -> None:
    """Mirror the sync state to the status file for the hook and the skill."""
    payload = {
        "timestamp": _iso_or_none(_now_utc()),
        "repo_url": state.repo_url,
        "visibility": state.visibility,
        "visibility_checked_at": _iso_or_none(state.visibility_checked_at),
        "is_push_allowed": state.is_push_allowed,
        "last_push_ok": state.last_push_ok,
        "last_push_at": _iso_or_none(state.last_push_at),
        "last_error": state.last_error,
    }
    status_path = status_file_path()
    try:
        status_path.write_text(json.dumps(payload, indent=2) + "\n")
    except OSError as e:
        logger.warning("Failed to write status file {}: {}", status_path, e)


def _do_tick(state: _SyncState) -> None:
    """Run one sync tick: commit runtime/ changes, verify visibility, push."""
    try:
        repo_url = load_repo_url()
    except GithubSyncConfigError as e:
        logger.error("Invalid sync config: {}", e)
        state.last_error = str(e)
        _write_status(state)
        return
    if repo_url is None:
        logger.warning(
            "github_sync.toml is missing; sync is not configured (run the "
            "github-sync skill), idling"
        )
        _write_status(state)
        return
    state.repo_url = repo_url

    if not is_runtime_worktree():
        # Self-healing path for a workspace recreated from a synced repo: the
        # wiring and worktree are container-local, so recreate them here once
        # the gateway/permissions allow it.
        apply_git_wiring()
        if not init_runtime_worktree():
            state.last_error = "runtime/ worktree init deferred (origin unreachable)"
            _write_status(state)
            return

    # Clear any stale index.lock first, so a commit interrupted by something
    # killing the process cannot wedge every future tick.
    _clear_stale_index_lock()
    _commit_runtime_changes()

    _refresh_visibility(state, repo_url)
    if state.is_push_allowed:
        _push_runtime(state, repo_url)
    elif state.visibility == VISIBILITY_PUBLIC:
        logger.error(
            "Sync repo {} is PUBLIC; refusing to push (make it private again "
            "to resume syncing)",
            repo_url,
        )
    else:
        logger.warning(
            "Sync repo {} visibility not confirmed yet; holding pushes", repo_url
        )
    _write_status(state)


def run_forever() -> None:
    """Main loop: poll runtime/ on a fixed interval and sync changes."""
    # Tee stderr-bound logs into LOG_FILE so operators can `tail` the file
    # across restarts of just this service window. /tmp wipes on container
    # restart, which is the intended scope for the debug log. Set up here
    # rather than at module import so that merely importing this module
    # (e.g. from tests) does not start writing to the log file.
    logger.add(LOG_FILE, level="INFO")

    logger.info("Starting github-sync (interval={}s)", TICK_INTERVAL_SECONDS)
    apply_git_wiring()

    state = _SyncState()
    while True:
        _do_tick(state)
        time.sleep(TICK_INTERVAL_SECONDS)
