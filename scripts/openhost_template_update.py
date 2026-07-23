#!/usr/bin/env python3
"""Detect an OpenHost app update and stage the new template for update-self.

OpenHost ships a new app version by rebuilding the image from its host-side
checkout of this repo and starting a fresh container; the persistent
``/mngr/`` volume (the mind's workspace, with its local edits) carries over
untouched. So a redeploy does NOT by itself change the code the mind runs --
the seed step deliberately never overwrites the volume.

This script bridges that gap. Because the image now carries the deployed
git history (see the Dockerfile / .dockerignore), three facts are available
on every boot:

  * ``/opt/openhost-template-version`` -- the commit SHA baked into THIS image
    (the version OpenHost just deployed).
  * ``/mngr/openhost_template_version`` -- the SHA the workspace last
    reconciled to (absent on a workspace that predates this mechanism).
  * ``/docker_build_code`` -- the new image's full source tree, INCLUDING its
    ``.git``, present until the first-boot seed cleans it up.

When the baked SHA differs from the stored one, an update is pending. This
script (run from the entrypoint BEFORE the seed cleanup) fetches the new
commit out of ``/docker_build_code`` into the live workspace repo as
``refs/openhost/incoming`` and drops a pending-update marker. The workspace
shares history with that commit (it was seeded from this same lineage), so
update-self can 3-way merge it into the mind's edits with a real merge base.
The system_interface picks up the marker on boot and asks the mind to run
update-self against the local ref -- no GitHub round-trip, and the reconcile
shows up in the chat.

``mark-reconciled`` is the completion side: update-self calls it once the
merge lands to record the new stored SHA and clear the marker.

Pure stdlib so it runs under a plain ``python3`` in the entrypoint, before
any venv exists.
"""

import argparse
import subprocess
import sys
from pathlib import Path

# Ref the incoming template commit is fetched to inside the workspace repo.
INCOMING_REF = "refs/openhost/incoming"


def read_sha(path: Path) -> str | None:
    """Return the stripped SHA in ``path``, or None when it is absent/empty."""
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    return text or None


def is_update_pending(baked_sha: str | None, stored_sha: str | None) -> bool:
    """An update is pending when a stored SHA exists and differs from the baked one.

    Both must be present. A missing baked SHA is an older image with no stamp --
    nothing to move to. A missing stored SHA is a fresh or legacy workspace that
    has no baseline yet: rather than reconcile against it (a legacy workspace was
    git-inited fresh and shares no history with the baked commit, so a merge
    would be an unrelated-histories mess), the caller adopts the baked SHA as the
    baseline via ``init_baseline`` and only genuine later updates -- stored
    present and different -- trigger a reconcile.
    """
    if baked_sha is None or stored_sha is None:
        return False
    return baked_sha != stored_sha


def init_baseline(*, baked_version_path: Path, stored_version_path: Path) -> bool:
    """Seed the stored SHA from the baked one when the workspace has no baseline.

    Run once per workspace right after the seed step: a fresh install adopts the
    version it was seeded from, and a legacy workspace (created before this
    mechanism) adopts the current image's version so only genuine future updates
    reconcile. No-op when a baseline already exists or the image has no stamp.
    Returns True when it wrote the baseline.
    """
    if read_sha(stored_version_path) is not None:
        return False
    baked_sha = read_sha(baked_version_path)
    if baked_sha is None:
        return False
    stored_version_path.write_text(f"{baked_sha}\n")
    return True


def _git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _has_git_repo(repo_dir: Path) -> bool:
    return (repo_dir / ".git").exists()


def capture_incoming(
    *,
    workspace_dir: Path,
    incoming_dir: Path,
    baked_version_path: Path,
    stored_version_path: Path,
    pending_marker_path: Path,
) -> bool:
    """Stage the new template commit for update-self if an update is pending.

    Returns True when an incoming ref was staged (an update is now pending),
    False when there was nothing to do (no update, first boot, or the source
    is unavailable). Idempotent: re-running after a stage re-fetches the same
    ref and rewrites the same marker.

    The workspace must already exist (a first-boot workspace is seeded directly
    from this image, so it needs no incoming ref). The incoming source
    (``/docker_build_code``) must be a git repo; it is on an update boot until
    the seed cleans it up, which is why the entrypoint runs this first.
    """
    if not _has_git_repo(workspace_dir):
        return False
    baked_sha = read_sha(baked_version_path)
    stored_sha = read_sha(stored_version_path)
    if not is_update_pending(baked_sha, stored_sha):
        return False
    if not _has_git_repo(incoming_dir):
        # The new source is gone (seed already ran) but the versions disagree.
        # Leave a marker so the reconcile can still be attempted from whatever
        # ref the workspace already has; do not fabricate an incoming ref.
        print(
            f"openhost_template_update: update pending ({stored_sha} -> {baked_sha}) "
            f"but incoming source {incoming_dir} is unavailable; marking anyway",
            file=sys.stderr,
        )
        pending_marker_path.write_text(f"{baked_sha}\n")
        return True

    fetch = _git(workspace_dir, "fetch", "--no-tags", str(incoming_dir), f"HEAD:{INCOMING_REF}")
    if fetch.returncode != 0:
        print(
            f"openhost_template_update: failed to fetch incoming template from "
            f"{incoming_dir}: {fetch.stderr.strip()}",
            file=sys.stderr,
        )
        return False
    pending_marker_path.write_text(f"{baked_sha}\n")
    print(
        f"openhost_template_update: staged update {stored_sha} -> {baked_sha} "
        f"at {INCOMING_REF}",
        file=sys.stderr,
    )
    return True


def mark_reconciled(
    *,
    stored_version_path: Path,
    pending_marker_path: Path,
    version: str,
) -> None:
    """Record ``version`` as the reconciled SHA and clear the pending marker."""
    stored_version_path.write_text(f"{version}\n")
    pending_marker_path.unlink(missing_ok=True)


def _cmd_capture(args: argparse.Namespace) -> int:
    staged = capture_incoming(
        workspace_dir=Path(args.workspace),
        incoming_dir=Path(args.incoming),
        baked_version_path=Path(args.baked),
        stored_version_path=Path(args.stored),
        pending_marker_path=Path(args.pending),
    )
    print("pending" if staged else "up-to-date")
    return 0


def _cmd_mark_reconciled(args: argparse.Namespace) -> int:
    mark_reconciled(
        stored_version_path=Path(args.stored),
        pending_marker_path=Path(args.pending),
        version=args.version,
    )
    return 0


def _cmd_init_baseline(args: argparse.Namespace) -> int:
    wrote = init_baseline(
        baked_version_path=Path(args.baked),
        stored_version_path=Path(args.stored),
    )
    print("initialized" if wrote else "already-set")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    capture = sub.add_parser("capture", help="Stage an incoming template update if one is pending")
    capture.add_argument("--workspace", default="/mngr/code")
    capture.add_argument("--incoming", default="/docker_build_code")
    capture.add_argument("--baked", default="/opt/openhost-template-version")
    capture.add_argument("--stored", default="/mngr/openhost_template_version")
    capture.add_argument("--pending", default="/mngr/openhost_update_pending")
    capture.set_defaults(func=_cmd_capture)

    reconciled = sub.add_parser("mark-reconciled", help="Record a completed reconcile")
    reconciled.add_argument("--stored", default="/mngr/openhost_template_version")
    reconciled.add_argument("--pending", default="/mngr/openhost_update_pending")
    reconciled.add_argument("--version", required=True)
    reconciled.set_defaults(func=_cmd_mark_reconciled)

    baseline = sub.add_parser("init-baseline", help="Adopt the baked SHA as baseline if unset")
    baseline.add_argument("--baked", default="/opt/openhost-template-version")
    baseline.add_argument("--stored", default="/mngr/openhost_template_version")
    baseline.set_defaults(func=_cmd_init_baseline)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
