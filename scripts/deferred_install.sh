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

# Fortress (tiliondev/fortress) stealth Chromium engine, replacing vanilla
# Playwright-managed Chromium. x64 from the official release; arm64 has no
# official release yet (tiliondev/fortress#28, open as of 2026-07-19) so this
# points at a fork build in the meantime -- swap _FORTRESS_ARM64_URL/_SHA256
# to the official tiliondev/fortress release once that PR merges.
# Fork build: https://github.com/MT-GoCode/fortress/releases/tag/linux-arm64-151.0.7908.0
# PR:         https://github.com/tiliondev/fortress/pull/28
readonly _FORTRESS_X64_URL="https://github.com/tiliondev/fortress/releases/download/v151.0.7908.0/tilion-fortress-linux-x64.tar.gz"
readonly _FORTRESS_X64_SHA256="243238b2b8a8b944b7ba2b63533d2b917da7d569dcb290ce96bf28151294b873"
readonly _FORTRESS_ARM64_URL="https://github.com/MT-GoCode/fortress/releases/download/linux-arm64-151.0.7908.0/tilion-fortress-linux-arm64.tar.gz"
readonly _FORTRESS_ARM64_SHA256="af83b768887161b22b1e06d0c0ba30b77ef10aec83ba68d59cbd333187f9cf78"
readonly _FORTRESS_INSTALL_DIR="/opt/fortress"

_install_fortress() {
    local marker
    marker="$(_marker_for fortress)"
    if [ -f "$marker" ]; then
        _log "fortress: marker present at $marker, skipping"
        return 0
    fi
    # `install-deps` apt-installs the shared libs Chromium needs (libnss3,
    # libgbm, etc.) -- Fortress is a Chromium build too, same requirement.
    # Recover any interrupted dpkg state first so a bake-interrupted install
    # can actually complete on retry.
    _recover_interrupted_dpkg
    _log "fortress: installing apt system libs"
    if ! (cd "$REPO_ROOT" && uv run playwright install-deps chromium); then
        _log "fortress: apt install FAILED; marker not written so the next boot retries"
        return 1
    fi

    local url sha256
    case "$(uname -m)" in
        x86_64) url="$_FORTRESS_X64_URL"; sha256="$_FORTRESS_X64_SHA256" ;;
        aarch64) url="$_FORTRESS_ARM64_URL"; sha256="$_FORTRESS_ARM64_SHA256" ;;
        *) _log "fortress: unsupported architecture $(uname -m)"; return 1 ;;
    esac
    _log "fortress: downloading $url"
    local tmp_dir
    tmp_dir="$(mktemp -d)"
    # shellcheck disable=SC2064
    trap "rm -rf '$tmp_dir'" RETURN
    local asset="$tmp_dir/fortress.tar.gz"
    if ! curl -fsSL -o "$asset" "$url"; then
        _log "fortress: download FAILED; marker not written so the next boot retries"
        return 1
    fi
    if [ "$(sha256sum "$asset" | awk '{print $1}')" != "$sha256" ]; then
        _log "fortress: SHA256 mismatch -- refusing to install"
        return 1
    fi
    rm -rf "$_FORTRESS_INSTALL_DIR"
    mkdir -p "$_FORTRESS_INSTALL_DIR"
    if ! tar xzf "$asset" -C "$_FORTRESS_INSTALL_DIR"; then
        _log "fortress: extract FAILED; marker not written so the next boot retries"
        return 1
    fi
    chmod +x "$_FORTRESS_INSTALL_DIR/tilion-fortress/tilion"
    touch "$marker"
    _log "fortress: install complete (${_FORTRESS_INSTALL_DIR}/tilion-fortress/tilion), marker written to $marker"
}

main() {
    mkdir -p "$MARKER_DIR"
    local rc=0
    _install_fortress || rc=$?
    if [ "$rc" -eq 0 ]; then
        _log "all deferred installs complete"
    else
        _log "one or more deferred installs failed (exit $rc); see logs above"
    fi
    return "$rc"
}

main "$@"
