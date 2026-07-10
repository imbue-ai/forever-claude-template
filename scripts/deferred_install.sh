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

# Both dirs are env-overridable for unit tests only; the supervisord program
# never sets them, so production always uses the container-local defaults.
readonly MARKER_DIR="${DEFERRED_INSTALL_MARKER_DIR:-/var/lib/minds/deferred-install}"
readonly REPO_ROOT=/mngr/code
readonly GITLEAKS_INSTALL_DIR="${GITLEAKS_INSTALL_DIR:-/usr/local/bin}"

# gitleaks release pin. The sha256s are hard-coded (never fetched at install
# time) from the checksums file published with the release:
# https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_checksums.txt
# Bump all three values together when upgrading.
readonly GITLEAKS_VERSION="8.30.1"
readonly GITLEAKS_SHA256_LINUX_X64="551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
readonly GITLEAKS_SHA256_LINUX_ARM64="e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080"

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

_sha256_of() {
    # Print a file's sha256 hex digest. Debian containers ship sha256sum;
    # the shasum fallback keeps the function runnable in macOS test envs.
    if command -v sha256sum > /dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    else
        shasum -a 256 "$1" | awk '{print $1}'
    fi
}

_gitleaks_asset_for_arch() {
    # Print "<release-asset-filename> <pinned-sha256>" for a `uname -m`
    # architecture; exits non-zero for architectures without a pinned binary.
    case "$1" in
        x86_64)
            printf '%s %s\n' "gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" "$GITLEAKS_SHA256_LINUX_X64"
            ;;
        aarch64 | arm64)
            printf '%s %s\n' "gitleaks_${GITLEAKS_VERSION}_linux_arm64.tar.gz" "$GITLEAKS_SHA256_LINUX_ARM64"
            ;;
        *)
            return 1
            ;;
    esac
}

_fetch_verify_install_gitleaks() {
    # Download a gitleaks release tarball from $1, verify it against the
    # pinned sha256 in $2, and install the extracted `gitleaks` binary to the
    # path in $3. All-or-nothing: any failure (download, checksum mismatch,
    # extract, install) leaves nothing installed and returns non-zero.
    local url="$1" expected_sha="$2" dest="$3"
    local tmpdir tarball actual_sha rc=0
    tmpdir="$(mktemp -d)"
    tarball="$tmpdir/gitleaks.tar.gz"
    if ! curl -fsSL --retry 3 -o "$tarball" "$url"; then
        _log "gitleaks: download failed: $url"
        rm -rf "$tmpdir"
        return 1
    fi
    actual_sha="$(_sha256_of "$tarball")"
    if [ "$actual_sha" != "$expected_sha" ]; then
        _log "gitleaks: sha256 MISMATCH for $url (expected ${expected_sha}, got ${actual_sha}); refusing to install"
        rm -rf "$tmpdir"
        return 1
    fi
    if ! tar -xzf "$tarball" -C "$tmpdir" gitleaks; then
        _log "gitleaks: could not extract the 'gitleaks' binary from the tarball"
        rc=1
    elif ! { mkdir -p "$(dirname "$dest")" && install -m 0755 "$tmpdir/gitleaks" "$dest"; }; then
        _log "gitleaks: failed to install binary to $dest"
        rc=1
    fi
    rm -rf "$tmpdir"
    return "$rc"
}

_install_gitleaks() {
    # Static secret-scanner binary (MIT, single Go binary) used by the
    # publish-inspiration skill's secret scan (.agents/skills/
    # publish-inspiration/scripts/build_inspiration.sh). That script falls
    # back to a grep-based scan while this install has not finished, so a
    # failure here degrades scan quality but never blocks a publish outright.
    local marker
    marker="$(_marker_for gitleaks)"
    if [ -f "$marker" ]; then
        _log "gitleaks: marker present at $marker, skipping"
        return 0
    fi
    local arch asset_and_sha asset sha url
    arch="$(uname -m)"
    if ! asset_and_sha="$(_gitleaks_asset_for_arch "$arch")"; then
        _log "gitleaks: no pinned binary for architecture '${arch}'; skipping (marker not written)"
        return 1
    fi
    asset="${asset_and_sha% *}"
    sha="${asset_and_sha#* }"
    url="https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/${asset}"
    _log "gitleaks: installing v${GITLEAKS_VERSION} from ${url}"
    if _fetch_verify_install_gitleaks "$url" "$sha" "$GITLEAKS_INSTALL_DIR/gitleaks"; then
        touch "$marker"
        _log "gitleaks: install complete, marker written to $marker"
    else
        _log "gitleaks: install FAILED; marker not written so the next boot retries"
        return 1
    fi
}

main() {
    mkdir -p "$MARKER_DIR"
    local rc=0
    # gitleaks first: a small single-binary download, so it becomes available
    # quickly instead of queueing behind playwright's multi-minute apt run.
    # Installs stay independent: each runs regardless of the others' results.
    _install_gitleaks || rc=$?
    _install_playwright || rc=$?
    if [ "$rc" -eq 0 ]; then
        _log "all deferred installs complete"
    else
        _log "one or more deferred installs failed (exit $rc); see logs above"
    fi
    return "$rc"
}

# Run main only when executed directly; unit tests source this file to
# exercise individual _install_<name> functions in isolation.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
