#!/usr/bin/env bash
# Idempotent installer for packages that are too heavy to bake into the
# Docker image but not required to start the chat agent or any boot-time
# service. Run once per container lifetime by the `deferred-install` entry
# in services.toml, gated by per-package marker files under
# /var/lib/minds/deferred-install/done.<package>.
#
# Designed to behave the same way across container restarts (no-op when
# the marker exists) and across fresh image builds (marker absent, install
# runs once). Crucially, this script never upgrades or reinstalls once a
# marker is present -- silent in-place version drift on restart is
# exactly what this pattern is avoiding. The agent decides when to upgrade.
#
# Add a new deferred package by adding a `_install_<name>` function and a
# matching call from `main`. Keep installs independent: a failure in one
# must not skip the others, and each must write its own per-package marker
# only on success.
set -euo pipefail

readonly MARKER_DIR=/var/lib/minds/deferred-install
readonly REPO_ROOT=/code

_log() {
    printf '[deferred-install] %s\n' "$*"
}

_marker_for() {
    printf '%s/done.%s\n' "$MARKER_DIR" "$1"
}

_install_playwright() {
    local marker
    marker="$(_marker_for playwright)"
    if [ -f "$marker" ]; then
        _log "playwright: marker present at $marker, skipping"
        return 0
    fi
    _log "playwright: installing chromium + apt system libs (this may take a few minutes)"
    # `--with-deps` apt-installs the system libraries chromium needs.
    # `uv run` uses the workspace venv (the playwright Python wheel is
    # already installed via the root pyproject.toml's pin). Subshell so
    # the cwd change does not leak to other `_install_<name>` functions.
    if (cd "$REPO_ROOT" && uv run playwright install --with-deps chromium); then
        touch "$marker"
        _log "playwright: install complete, marker written to $marker"
    else
        _log "playwright: install FAILED; marker not written so the next boot retries"
        return 1
    fi
}

main() {
    mkdir -p "$MARKER_DIR"
    local rc=0
    _install_playwright || rc=$?
    if [ "$rc" -eq 0 ]; then
        _log "all deferred installs complete"
    else
        _log "one or more deferred installs failed (exit $rc); see logs above"
    fi
    return "$rc"
}

main "$@"
