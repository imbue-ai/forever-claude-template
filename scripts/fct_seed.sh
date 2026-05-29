#!/bin/sh
# First-boot seed script for forever-claude-template containers.
#
# Run synchronously by mngr (via the `post_host_create_command` create-
# template hook) once the host is online but before any agent work_dir
# setup. Responsibilities:
#
#   1. Seed the /mngr/ persistent volume with the baked workspace using
#      an atomic two-step move via /mngr/code.moving so a crash mid-copy
#      never leaves /mngr/code half-populated. The workspace was baked
#      into /mngr/code at image-build time and then renamed to
#      /docker_build_code at the very end of the build (so the runtime
#      /mngr/ volume mount path is empty in the shipped image); this
#      script relocates it onto the volume.
#
#   2. Clean up /docker_build_code after seeding succeeds, so it doesn't
#      keep occupying overlay space on the running container.
#
#   3. Ensure /mngr/worktree/ exists so the /worktree -> /mngr/worktree
#      safety-net symlink (created in the image layer by the Dockerfile)
#      always resolves, even on a fresh volume with no worktrees yet.
#
# This script is installed at /usr/local/bin/fct-seed by an image-layer
# COPY (not via the volume-bound /mngr/code/) so that it is available
# before the seed step itself runs.
#
# Race-free: mngr blocks on this command's exit before issuing any other
# work that touches /mngr/. No PID-1 / signal-handling logic here -- the
# container's long-running CMD (mngr's generic keep-alive) handles that.

SEED_SOURCE=/docker_build_code
SEED_STAGING=/mngr/code.moving
SEED_TARGET=/mngr/code

seed_workspace_onto_volume() {
    # Warm-boot fast path: the workspace is already on the volume. Do not
    # touch it -- the agent may have made local edits we must not
    # overwrite.
    if [ -d "$SEED_TARGET" ] && [ -n "$(ls -A "$SEED_TARGET" 2>/dev/null)" ]; then
        return 0
    fi

    # A prior boot crashed between staging and the atomic rename. Wipe
    # the half-staged copy and re-stage from /docker_build_code below.
    if [ -e "$SEED_STAGING" ]; then
        echo "fct-seed: wiping stale $SEED_STAGING from a prior interrupted seed"
        rm -rf "$SEED_STAGING"
    fi

    # Broken-volume case: the image's seed source is gone AND the volume
    # has neither the final nor the staged copy. Fail loudly so the
    # issue surfaces in mngr/docker logs, rather than the container
    # silently sleeping forever with no workspace.
    if [ ! -e "$SEED_SOURCE" ]; then
        echo "fct-seed: ERROR: $SEED_TARGET missing AND $SEED_SOURCE missing -- volume is in a broken state and cannot be seeded" >&2
        exit 1
    fi

    # Stage: cross-filesystem copy from the image layer onto the volume.
    # `cp -a` preserves mode/owner/timestamps. Land on a sibling path so
    # the final rename below is a single inode-level operation on the
    # same filesystem.
    echo "fct-seed: staging $SEED_SOURCE -> $SEED_STAGING"
    cp -a "$SEED_SOURCE" "$SEED_STAGING"

    # Remove any pre-existing empty target so the atomic mv below
    # replaces it cleanly. `mv src dst` when dst is an existing
    # directory moves src INTO dst (dst/src) rather than replacing dst,
    # which would land the workspace at /mngr/code/code.moving instead
    # of /mngr/code/. `rmdir` only succeeds on empty directories, so
    # this is safe -- we already early-returned above if the target was
    # non-empty.
    rmdir "$SEED_TARGET" 2>/dev/null || true

    # Commit: atomic rename. Either fully succeeds or doesn't happen at
    # all, so an interrupted seed either has the workspace fully in
    # place or still has /mngr/code.moving to re-stage from on the next
    # invocation.
    echo "fct-seed: atomic-renaming $SEED_STAGING -> $SEED_TARGET"
    mv "$SEED_STAGING" "$SEED_TARGET"
}

cleanup_seed_source() {
    # Only safe to remove the image-layer source AFTER the volume target
    # is in place. Skip silently if a prior seed already cleaned it up.
    if [ -e "$SEED_SOURCE" ]; then
        echo "fct-seed: cleaning up $SEED_SOURCE"
        rm -rf "$SEED_SOURCE"
    fi
}

ensure_worktree_dir() {
    # Idempotent. Guarantees the target of the /worktree -> /mngr/worktree
    # safety-net symlink always exists.
    mkdir -p /mngr/worktree
}

set -e
seed_workspace_onto_volume
cleanup_seed_source
ensure_worktree_dir
