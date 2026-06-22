#!/usr/bin/env bash
# Idempotent installer for packages that are too heavy to bake into the
# Docker image but not required to start the chat agent or any boot-time
# service. Run once per container lifetime by the one-shot `deferred-install`
# supervisord program, gated by per-package marker files under
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
readonly REPO_ROOT=/mngr/code

_log() {
    printf '[deferred-install] %s\n' "$*"
}

_marker_for() {
    printf '%s/done.%s\n' "$MARKER_DIR" "$1"
}

_recover_interrupted_dpkg() {
    # A prior apt/dpkg run killed mid-operation leaves dpkg broken, after which
    # every `apt-get install` aborts. This happens routinely for pool hosts: the
    # bake's `mngr stop` parks the host while this deferred install's first-boot
    # `apt` is still running, so the post-lease retry would otherwise fail
    # forever. Recover up front -- each step is a fast no-op when dpkg is already
    # consistent. Best-effort: we log failures loudly (not silently) and let the
    # apt step below surface the real error.
    #
    # Two distinct breakages need two different repairs:
    #   1. Killed during *configure*: packages are unpacked-but-unconfigured;
    #      `dpkg --configure -a` finishes them.
    #   2. Killed during *unpack*: the half-unpacked package gets dpkg's
    #      reinst-required ("R") flag (the "very bad inconsistent state" error),
    #      which `dpkg --configure -a` CANNOT fix -- it skips reinst-required
    #      packages. Those must be reinstalled.
    if ! dpkg --configure -a; then
        _log "WARNING: 'dpkg --configure -a' returned non-zero; continuing with broken-package repair"
    fi
    # Reinstall any package left reinst-required (the 3rd char of dpkg's status
    # abbreviation is "R"); only a reinstall repairs a half-unpacked package.
    local reinst_required
    reinst_required="$(dpkg-query -W -f '${Package} ${db:Status-Abbrev}\n' 2>/dev/null \
        | awk 'substr($2, 3, 1) == "R" { print $1 }')"
    if [ -n "$reinst_required" ]; then
        # shellcheck disable=SC2086
        _log "reinstalling packages left reinst-required by an interrupted unpack: $(echo $reinst_required | tr '\n' ' ')"
        # shellcheck disable=SC2086
        if ! apt-get install --reinstall -y $reinst_required; then
            _log "WARNING: reinstall of reinst-required packages returned non-zero; the apt install below may still fail"
        fi
    fi
    # Finally, let apt repair any remaining broken dependencies (no-op when clean).
    if ! apt-get --fix-broken install -y; then
        _log "WARNING: 'apt-get --fix-broken install' returned non-zero; the apt install below may still fail"
    fi
}

_install_playwright() {
    local marker
    marker="$(_marker_for playwright)"
    if [ -f "$marker" ]; then
        _log "playwright: marker present at $marker, skipping"
        return 0
    fi
    # `playwright install --with-deps` shells out to apt; recover any
    # interrupted dpkg state first so an install the bake interrupted can
    # actually complete on retry.
    _recover_interrupted_dpkg
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
