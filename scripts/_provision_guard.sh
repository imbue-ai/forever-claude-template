#!/usr/bin/env bash
# Content-addressed provisioning skip guard (issue 2306).
#
# A provisioning step whose effects are GLOBAL (system packages, /usr/local/bin
# binaries, /root/.local tools) is a pure function of the repo content: running
# it again on the *identical* tree reproduces the same VM state. So we fingerprint
# the repo by its git tree hash and drop a marker once the step completes; a later
# run whose tree matches the marker skips it. The pre-baked Lima image's bake runs
# the step on the release tree, so a create that boots that image for the *same*
# tree finds the marker and skips it.
#
# IMPORTANT -- only guard steps with GLOBAL effects (e.g. setup_system). Do NOT
# guard steps that write outputs INTO the workspace repo (install_dependencies ->
# .venv/node_modules, build_workspace -> frontend dist): the create re-materializes
# /mngr/code from a git-mirror push (tracked files only), so those in-repo outputs
# are absent at create time and MUST be regenerated every create. Skipping them
# leaves the workspace half-built (e.g. "Frontend not built").
#
# Safe by construction -- it NEVER skips unless it can prove the identical tree
# was already provisioned:
#   * no git repo at the canonical path (e.g. the Docker build, which runs these
#     before any repo exists), not a git tree, or no marker  -> run normally.
#   * the tree hash covers the scripts themselves plus every lockfile and
#     vendored file, so any content change invalidates the marker.
#
# Usage (source it, then):
#   provision_skip_if_done <name>   # early-exits the calling script when matched
#   provision_mark_done <name>      # call at the end, after a successful run

# Strict mode. This file is sourced by callers that already set this (e.g.
# setup_system.sh), so re-asserting it here is a no-op for them and keeps the
# library safe to source from anywhere.
set -euo pipefail

# Canonical workspace repo location in the VM: the Lima bake clones here and a
# create syncs the workspace here. Overridable for tests.
_PROVISION_REPO_ROOT="${PROVISION_REPO_ROOT:-/mngr/code}"
_PROVISION_MARKER_DIR="${PROVISION_MARKER_DIR:-/var/lib/minds/provision}"

_provision_tree_fingerprint() {
    git -C "$_PROVISION_REPO_ROOT" rev-parse "HEAD^{tree}" 2>/dev/null || true
}

provision_skip_if_done() {
    _name="$1"
    _fp="$(_provision_tree_fingerprint)"
    # No resolvable tree -> we cannot prove a match, so let the caller run.
    [ -n "$_fp" ] || return 0
    if [ -f "$_PROVISION_MARKER_DIR/$_fp.$_name.done" ]; then
        echo "[provision-guard] $_name already provisioned for tree $_fp; skipping."
        exit 0
    fi
}

provision_mark_done() {
    _name="$1"
    _fp="$(_provision_tree_fingerprint)"
    [ -n "$_fp" ] || return 0
    mkdir -p "$_PROVISION_MARKER_DIR"
    : > "$_PROVISION_MARKER_DIR/$_fp.$_name.done"
}
