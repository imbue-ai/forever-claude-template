"""Setup and restore of runtime/ as a git worktree on the runtime-sync branch.

Run by the github-sync skill at enable time, and retried by the service on
every tick while runtime/ is not yet a worktree (the self-healing path for a
workspace recreated from a previously-synced repo: once the user re-grants
the latchkey GitHub permissions, the next tick fetches origin's runtime-sync
branch and materializes the prior runtime/ state -- memory, tickets,
transcripts -- automatically).

Any files already sitting in runtime/ (a fresh boot writes signal files and
tickets there before sync is enabled) are staged aside during worktree
creation and restored on top afterwards, never clobbering restored state.
"""

import os
import shutil
import subprocess

from loguru import logger

from github_sync.config import RUNTIME_DIR, SYNC_BRANCH

SYNC_USER_NAME = "github-sync"
SYNC_USER_EMAIL = "github-sync@minds.local"

_RUNTIME_PREEXISTING_DIR = RUNTIME_DIR.with_name(RUNTIME_DIR.name + ".preexisting")

# `git ls-remote --exit-code` exits 2 when the remote is reachable but has no
# matching refs.
_LS_REMOTE_NO_MATCHING_REFS = 2


def _git_noninteractive_env() -> dict[str, str]:
    """Environment for sync git calls: never prompt for credentials.

    GIT_TERMINAL_PROMPT=0 turns any credential prompt (e.g. a fetch through a
    gateway whose permission grant lapsed) into a fast failure instead of a
    TTY prompt that would wedge the caller forever; every caller here already
    logs-and-continues on a nonzero exit.
    """
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


def git_main(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command in the main checkout, never raising or prompting."""
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=False,
        env=_git_noninteractive_env(),
    )


def git_runtime(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command inside the runtime worktree, never raising or prompting."""
    return subprocess.run(
        ["git", "-C", str(RUNTIME_DIR), *args],
        capture_output=True,
        text=True,
        check=False,
        env=_git_noninteractive_env(),
    )


def is_runtime_worktree() -> bool:
    """Return True when runtime/ is already a git worktree."""
    return (RUNTIME_DIR / ".git").exists()


def _restore_preexisting_into_worktree() -> None:
    """Move any files from runtime.preexisting/ back into runtime/."""
    if not _RUNTIME_PREEXISTING_DIR.exists():
        return
    for entry in list(_RUNTIME_PREEXISTING_DIR.iterdir()):
        target = RUNTIME_DIR / entry.name
        if target.exists():
            # Don't clobber what the worktree already has (e.g. restored
            # state fetched from origin, or a fresh .gitignore).
            continue
        shutil.move(str(entry), str(target))
    try:
        _RUNTIME_PREEXISTING_DIR.rmdir()
    except OSError:
        logger.warning(
            "{} not empty after restore; leaving for inspection",
            _RUNTIME_PREEXISTING_DIR,
        )


def _stage_preexisting_aside() -> None:
    """Move runtime/'s contents to runtime.preexisting/ so we can add a worktree.

    Only called when runtime/ exists with files but is not yet a git worktree.
    """
    if _RUNTIME_PREEXISTING_DIR.exists():
        # Stale leftover from a prior failed init -- clear it.
        shutil.rmtree(_RUNTIME_PREEXISTING_DIR)
    shutil.move(str(RUNTIME_DIR), str(_RUNTIME_PREEXISTING_DIR))


def _runtime_dir_has_files() -> bool:
    """Return True if runtime/ exists and contains anything."""
    if not RUNTIME_DIR.exists():
        return False
    return any(RUNTIME_DIR.iterdir())


def _create_orphan_runtime_worktree(branch: str) -> subprocess.CompletedProcess[str]:
    """Add runtime/ as a worktree on a fresh orphan branch, git-version-agnostically.

    `git worktree add --orphan` only exists in git >= 2.42, but the Lima
    provider's Debian 12 base ships git 2.39. So build the orphan branch with
    plumbing that has worked for ages -- a parentless commit on the empty tree --
    then do a normal `git worktree add` for it. Returns the final worktree-add
    CompletedProcess; if an earlier plumbing step fails, returns that failing
    CompletedProcess so the caller's existing error handling fires.
    """
    empty_tree = git_main("hash-object", "-w", "-t", "tree", "/dev/null")
    if empty_tree.returncode != 0:
        return empty_tree
    # Commit identity is passed via -c because the container may have no global
    # git identity yet, and commit-tree refuses to run without one.
    orphan_commit = git_main(
        "-c",
        f"user.name={SYNC_USER_NAME}",
        "-c",
        f"user.email={SYNC_USER_EMAIL}",
        "commit-tree",
        empty_tree.stdout.strip(),
        "-m",
        "runtime sync: init",
    )
    if orphan_commit.returncode != 0:
        return orphan_commit
    branch_result = git_main("branch", branch, orphan_commit.stdout.strip())
    if branch_result.returncode != 0:
        return branch_result
    return git_main("worktree", "add", str(RUNTIME_DIR), branch)


def init_runtime_worktree() -> bool:
    """One-time setup of runtime/ as a worktree of the runtime-sync branch.

    Returns True when runtime/ is a worktree afterwards. Never raises: a
    failure (most commonly the origin remote being unreachable because the
    latchkey GitHub permission has not been granted yet) is logged and the
    caller retries later. Deliberately refuses to create a *fresh* orphan
    branch while origin is unreachable -- a remote runtime-sync branch might
    exist (workspace recreated from a synced repo), and starting an unrelated
    local history would leave the two permanently diverged.
    """
    if is_runtime_worktree():
        # A prior init may have staged runtime/ content aside and been killed
        # before restoring it. Recover that content now rather than stranding
        # it; this no-ops when runtime.preexisting/ is absent (the common case).
        _restore_preexisting_into_worktree()
        return True

    local_branch_exists = (
        git_main("rev-parse", "--verify", "--quiet", SYNC_BRANCH).returncode == 0
    )

    # Classify the remote state: branch exists (restore it), remote reachable
    # but branch absent (fresh orphan), or remote unreachable (retry later).
    ls_remote = git_main(
        "ls-remote", "--exit-code", "--heads", "origin", SYNC_BRANCH
    )
    if ls_remote.returncode == 0:
        has_remote_branch = True
    elif ls_remote.returncode == _LS_REMOTE_NO_MATCHING_REFS:
        has_remote_branch = False
    elif local_branch_exists:
        # Offline, but a local runtime-sync branch already exists (e.g. a
        # prior init whose worktree was removed) -- reattaching to it cannot
        # diverge from origin any further than it already has.
        has_remote_branch = False
    else:
        logger.warning(
            "origin is unreachable (rc={}): {}; deferring runtime worktree init",
            ls_remote.returncode,
            ls_remote.stderr.strip(),
        )
        return False

    logger.info("Initializing runtime worktree on branch {}", SYNC_BRANCH)

    remote_ref = f"origin/{SYNC_BRANCH}"
    if has_remote_branch:
        fetch_result = git_main("fetch", "origin", SYNC_BRANCH)
        if fetch_result.returncode != 0:
            logger.warning(
                "git fetch origin {} failed (rc={}): {}; deferring init",
                SYNC_BRANCH,
                fetch_result.returncode,
                fetch_result.stderr.strip(),
            )
            return False

    staged_aside = False
    if _runtime_dir_has_files():
        logger.info(
            "runtime/ already has files; staging them aside before adding the worktree"
        )
        _stage_preexisting_aside()
        staged_aside = True

    if has_remote_branch:
        result = git_main(
            "worktree", "add", "-B", SYNC_BRANCH, str(RUNTIME_DIR), remote_ref
        )
    elif local_branch_exists:
        result = git_main("worktree", "add", str(RUNTIME_DIR), SYNC_BRANCH)
    else:
        result = _create_orphan_runtime_worktree(SYNC_BRANCH)

    if result.returncode != 0:
        logger.error(
            "git worktree add failed (rc={}): {}",
            result.returncode,
            result.stderr.strip(),
        )
        # Restore preexisting files so other services don't lose them.
        if staged_aside:
            if not RUNTIME_DIR.exists():
                shutil.move(str(_RUNTIME_PREEXISTING_DIR), str(RUNTIME_DIR))
            else:
                _restore_preexisting_into_worktree()
        return False

    # Configure bot identity for sync commits inside this worktree only.
    git_runtime("config", "user.name", SYNC_USER_NAME)
    git_runtime("config", "user.email", SYNC_USER_EMAIL)

    if has_remote_branch:
        # Make sure the local branch tracks the remote (some git versions
        # don't set this automatically with -B + an explicit ref).
        git_runtime("branch", "--set-upstream-to", remote_ref)
    elif not local_branch_exists:
        # Fresh orphan branch: write the .gitignore for secrets and make an
        # initial commit so push has something to push. runtime/secrets holds
        # e.g. the Cloudflare tunnel token, which must never reach the remote.
        gitignore = RUNTIME_DIR / ".gitignore"
        gitignore.write_text("secrets\n")
        git_runtime("add", ".gitignore")
        commit = git_runtime("commit", "-m", "runtime sync: init")
        if commit.returncode != 0:
            logger.error(
                "initial commit failed (rc={}): {}",
                commit.returncode,
                commit.stderr.strip(),
            )

    # Restore staged-aside content. Calling unconditionally (rather than
    # gating on the `staged_aside` flag) also recovers content left by a
    # prior init that staged aside but was killed before it could restore.
    _restore_preexisting_into_worktree()
    return True
