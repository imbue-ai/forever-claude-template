#!/bin/sh
# First-boot seed-and-wait entrypoint for forever-claude-template containers.
#
# Runs as PID 1 (invoked via Dockerfile CMD). Responsibilities:
#
#   1. Seed the /mngr/ persistent volume with the baked workspace on first
#      boot, using an atomic two-step move via /mngr/code.moving so a crash
#      mid-copy never leaves /mngr/code half-populated. The workspace was
#      baked into /mngr/code at image-build time and then renamed to
#      /docker_build_code at the very end of the build (so the runtime
#      /mngr/ volume mount path is empty in the shipped image); this script
#      relocates it onto the volume on first boot.
#
#   2. Clean up /docker_build_code after seeding succeeds, so it doesn't keep
#      occupying overlay space on the running container.
#
#   3. Ensure /mngr/worktree/ exists on every boot so the
#      /worktree -> /mngr/worktree safety-net symlink (created in the image
#      layer by the Dockerfile) always resolves, even on a fresh volume
#      with no worktrees yet.
#
#   4. Sleep forever while staying responsive to SIGTERM. PID 1 must
#      explicitly install signal handlers; otherwise `docker stop` waits
#      the full SIGKILL timeout. We trap SIGTERM and exit 0 cleanly.
#
# This script is installed at /usr/local/bin/fct-entrypoint.sh by an
# image-layer COPY (not via the volume-bound /mngr/code/) so that it is
# available before the seed step runs.

SEED_SOURCE=/docker_build_code
SEED_STAGING=/mngr/code.moving
SEED_TARGET=/mngr/code

seed_workspace_onto_volume() {
    # Warm-boot fast path: the workspace is already on the volume. Do not
    # touch it -- the agent may have made local edits we must not overwrite.
    if [ -e "$SEED_TARGET" ]; then
        return 0
    fi

    # A prior boot crashed between staging and the atomic rename. Wipe the
    # half-staged copy and re-stage from /docker_build_code below.
    if [ -e "$SEED_STAGING" ]; then
        echo "fct-seed: wiping stale $SEED_STAGING from a prior interrupted seed"
        rm -rf "$SEED_STAGING"
    fi

    # Broken-volume case: the image's seed source is gone AND the volume
    # has neither the final nor the staged copy. Fail loudly so the issue
    # surfaces in mngr/docker logs, rather than the container silently
    # sleeping forever with no workspace.
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

    # Commit: atomic rename. Either fully succeeds or doesn't happen at
    # all, so an interrupted boot either has the workspace fully in place
    # or still has /mngr/code.moving to re-stage from on the next boot.
    echo "fct-seed: atomic-renaming $SEED_STAGING -> $SEED_TARGET"
    mv "$SEED_STAGING" "$SEED_TARGET"
}

cleanup_seed_source() {
    # Only safe to remove the image-layer source AFTER the volume target
    # is in place. Skip silently if a prior boot already cleaned it up.
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

wait_for_sigterm() {
    # PID 1 signal handling. `tail -f /dev/null` alone would not install
    # signal handlers; the `trap` makes `docker stop` exit cleanly and
    # quickly instead of waiting for the SIGKILL timeout.
    trap 'exit 0' TERM
    tail -f /dev/null &
    wait
}

# Fail fast on any seed-step error so the broken-volume case surfaces.
# The wait loop runs without `-e` because a signal-interrupted `wait`
# can return non-zero, which is normal (the trap exits 0 first anyway).
set -e
seed_workspace_onto_volume
cleanup_seed_source
ensure_worktree_dir
set +e
wait_for_sigterm
