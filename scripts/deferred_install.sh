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

_install_playwright_deps() {
    local marker
    marker="$(_marker_for playwright_deps)"
    if [ -f "$marker" ]; then
        _log "playwright_deps: marker present at $marker, skipping"
        return 0
    fi
    # `install-deps` only apt-installs the shared system libraries a Chromium
    # build needs (libnss3, libgbm, etc.) -- it does not download a browser
    # binary. Both engines below are Chromium builds and need these libs, so
    # this runs once, ahead of either. Recover any interrupted dpkg state
    # first so an install the bake interrupted can actually complete on retry.
    _recover_interrupted_dpkg
    _log "playwright_deps: installing apt system libs (this may take a few minutes)"
    if (cd "$REPO_ROOT" && uv run playwright install-deps chromium); then
        touch "$marker"
        _log "playwright_deps: install complete, marker written to $marker"
    else
        _log "playwright_deps: install FAILED; marker not written so the next boot retries"
        return 1
    fi
}

# CloakBrowser is a from-source C++ (Blink/V8) stealth patch of Chromium --
# used everywhere a Chromium binary is launched in this image (the agentic
# browser fleet, and any agent's own direct Playwright calls), replacing
# vanilla Chromium entirely. See libs/browser/README.md.
#
# Pinned to a specific free-tier release (not `latest`): CloakBrowser's
# newest major version is gated behind a paid tier, so `latest` would 404 on
# the asset for an unpaid image. Bump deliberately by updating these three
# vars together (and the SHA256s below) -- never silently drift on restart,
# matching every other deferred package's contract.
readonly _CLOAKBROWSER_VERSION="chromium-v146.0.7680.177.4"
readonly _CLOAKBROWSER_INSTALL_DIR="/opt/cloakbrowser"
readonly _CLOAKBROWSER_RELEASE_URL="https://github.com/CloakHQ/CloakBrowser/releases/download/${_CLOAKBROWSER_VERSION}"
# From that release's SHA256SUMS; recompute + update on every version bump.
readonly _CLOAKBROWSER_SHA256_ARM64="8b71ce53b4fd131327331a31fba3835d71882d19bfaabde78dd0f5390bd16f45"
readonly _CLOAKBROWSER_SHA256_X64="5af027faafb1fef9933eb784c094b764706de22a372a2cee84bc117fc4ab537f"

_cloakbrowser_asset_for_arch() {
    # Maps `uname -m` to CloakBrowser's release asset naming + pinned hash.
    case "$(uname -m)" in
        aarch64|arm64)
            printf 'cloakbrowser-linux-arm64.tar.gz %s\n' "$_CLOAKBROWSER_SHA256_ARM64"
            ;;
        x86_64|amd64)
            printf 'cloakbrowser-linux-x64.tar.gz %s\n' "$_CLOAKBROWSER_SHA256_X64"
            ;;
        *)
            _log "cloakbrowser: unsupported architecture $(uname -m)"
            return 1
            ;;
    esac
}

_install_cloakbrowser() {
    local marker
    marker="$(_marker_for cloakbrowser)"
    if [ -f "$marker" ]; then
        _log "cloakbrowser: marker present at $marker, skipping"
        return 0
    fi
    local asset expected_sha256
    read -r asset expected_sha256 < <(_cloakbrowser_asset_for_arch) || return 1
    _log "cloakbrowser: downloading ${_CLOAKBROWSER_VERSION}/${asset}"
    local tmp_dir
    tmp_dir="$(mktemp -d)"
    # shellcheck disable=SC2064
    trap "rm -rf '$tmp_dir'" RETURN
    if ! curl -fsSL -o "$tmp_dir/$asset" "${_CLOAKBROWSER_RELEASE_URL}/${asset}"; then
        _log "cloakbrowser: download FAILED; marker not written so the next boot retries"
        return 1
    fi
    local actual_sha256
    actual_sha256="$(sha256sum "$tmp_dir/$asset" | awk '{print $1}')"
    if [ "$actual_sha256" != "$expected_sha256" ]; then
        _log "cloakbrowser: SHA256 mismatch for $asset (expected $expected_sha256, got $actual_sha256) -- refusing to install"
        return 1
    fi
    rm -rf "$_CLOAKBROWSER_INSTALL_DIR"
    mkdir -p "$_CLOAKBROWSER_INSTALL_DIR"
    if ! tar xzf "$tmp_dir/$asset" -C "$_CLOAKBROWSER_INSTALL_DIR"; then
        _log "cloakbrowser: extract FAILED; marker not written so the next boot retries"
        return 1
    fi
    chmod +x "$_CLOAKBROWSER_INSTALL_DIR/chrome"
    touch "$marker"
    _log "cloakbrowser: install complete (${_CLOAKBROWSER_INSTALL_DIR}/chrome), marker written to $marker"
}

main() {
    mkdir -p "$MARKER_DIR"
    local rc=0
    _install_playwright_deps || rc=$?
    _install_cloakbrowser || rc=$?
    if [ "$rc" -eq 0 ]; then
        _log "all deferred installs complete"
    else
        _log "one or more deferred installs failed (exit $rc); see logs above"
    fi
    return "$rc"
}

main "$@"
