#!/usr/bin/env bash
# Pinned installer for the two secret-scanner binaries the
# publish-inspiration skill's scan gate (scan_secrets.sh) hard-requires:
#
#   - betterleaks (MIT)        -- gitleaks' successor, by the gitleaks author
#   - kingfisher  (Apache-2.0) -- MongoDB's scanner; the scan gate always
#                                 runs it with --no-validate
#
# This file is the single source of truth for the version pins and per-arch
# sha256s. It is invoked by the common scripts/setup_system.sh (which the
# Dockerfile RUNs at image-build time and the Lima provider runs directly in
# the VM), so every workspace -- docker-built or Lima-provisioned -- has both
# binaries from second zero. If a binary is ever missing (an environment not
# built that way, or a failed bake), run this script by hand to install both
# -- the skip-when-pinned check below makes an already-satisfied run an
# instant no-op.
#
# Idempotent and cheap when already satisfied: a tool whose binary exists in
# the install dir AND reports the pinned version is skipped without any
# network access. Installs are isolated per tool: a failure in one never
# skips the others, and the exit code is non-zero if any requested install
# failed. Each sha256 is hard-coded (never fetched at install time) from the
# release's published checksums; a mismatch refuses to install.
#
# Usage: install_secret_scanners.sh [tool ...]   (default: both)
set -euo pipefail

# Env-overridable for unit tests only; production always installs to the
# default. The binaries land as $SECRET_SCANNER_INSTALL_DIR/<tool>.
readonly SECRET_SCANNER_INSTALL_DIR="${SECRET_SCANNER_INSTALL_DIR:-/usr/local/bin}"

# Release pins. The sha256s come from each release's published checksums:
#   betterleaks: https://github.com/betterleaks/betterleaks/releases/download/v1.6.1/checksums.txt
#   kingfisher:  https://github.com/mongodb/kingfisher/releases/download/v1.106.0/multiple.intoto.jsonl
#                (kingfisher publishes no plain checksums.txt; the sha256s are
#                the subject digests in that sigstore/in-toto attestation)
# Bump a tool's version and both of its sha256s together when upgrading.
readonly BETTERLEAKS_VERSION="1.6.1"
readonly BETTERLEAKS_SHA256_LINUX_X64="fbefc700a0bd4522cc952dd2a8f259cdb80526d7e60114aca19bb2d6fdc80f81"
readonly BETTERLEAKS_SHA256_LINUX_ARM64="bab9688ba968264ace67b608fc7a7d8f5e61218cde70029d32cbc894e3808fdf"
readonly KINGFISHER_VERSION="1.106.0"
readonly KINGFISHER_SHA256_LINUX_X64="5320b7a3a2f7a8c9b90990ee90099d70903d84c302e61b54dae87b7000c8c153"
readonly KINGFISHER_SHA256_LINUX_ARM64="1c888a174b4fa8eaebbddf4b46829f4d00079f44d8b07ed4f13d048fa6068540"

_log() {
    printf '[install-secret-scanners] %s\n' "$*"
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

_scanner_pinned_version() {
    case "$1" in
        betterleaks) printf '%s\n' "$BETTERLEAKS_VERSION" ;;
        kingfisher) printf '%s\n' "$KINGFISHER_VERSION" ;;
        *) return 1 ;;
    esac
}

_scanner_asset_for_arch() {
    # Print "<release-asset-filename> <pinned-sha256>" for tool $1 on a
    # `uname -m` architecture $2; exits non-zero for architectures without a
    # pinned binary. Each project names its linux assets differently
    # (x64/arm64 vs amd64/arm64, underscores vs dashes, .tar.gz vs .tgz).
    local tool="$1" arch="$2" norm
    case "$arch" in
        x86_64) norm="x64" ;;
        aarch64 | arm64) norm="arm64" ;;
        *) return 1 ;;
    esac
    case "${tool}:${norm}" in
        betterleaks:x64)
            printf '%s %s\n' "betterleaks_${BETTERLEAKS_VERSION}_linux_x64.tar.gz" "$BETTERLEAKS_SHA256_LINUX_X64"
            ;;
        betterleaks:arm64)
            printf '%s %s\n' "betterleaks_${BETTERLEAKS_VERSION}_linux_arm64.tar.gz" "$BETTERLEAKS_SHA256_LINUX_ARM64"
            ;;
        kingfisher:x64)
            printf '%s %s\n' "kingfisher-linux-x64.tgz" "$KINGFISHER_SHA256_LINUX_X64"
            ;;
        kingfisher:arm64)
            printf '%s %s\n' "kingfisher-linux-arm64.tgz" "$KINGFISHER_SHA256_LINUX_ARM64"
            ;;
        *)
            return 1
            ;;
    esac
}

_scanner_release_url() {
    # Print the release download URL for tool $1's asset filename $2.
    local tool="$1" asset="$2"
    case "$tool" in
        betterleaks)
            printf 'https://github.com/betterleaks/betterleaks/releases/download/v%s/%s\n' "$BETTERLEAKS_VERSION" "$asset"
            ;;
        kingfisher)
            printf 'https://github.com/mongodb/kingfisher/releases/download/v%s/%s\n' "$KINGFISHER_VERSION" "$asset"
            ;;
        *)
            return 1
            ;;
    esac
}

_scanner_at_pinned_version() {
    # True when the tool's binary already exists in the install dir and its
    # --version output mentions the pinned version (both print it:
    # "betterleaks version 1.6.1" / "kingfisher 1.106.0").
    local tool="$1" bin version_output
    bin="$SECRET_SCANNER_INSTALL_DIR/$tool"
    [ -x "$bin" ] || return 1
    version_output="$("$bin" --version 2>&1 || true)"
    printf '%s' "$version_output" | grep -qF "$(_scanner_pinned_version "$tool")"
}

_fetch_verify_install() {
    # Download tool $1's release tarball from $2, verify it against the pinned
    # sha256 in $3, and install the extracted binary (named after the tool at
    # the archive root in both projects' tarballs) to the path in $4.
    # All-or-nothing: any failure (download, checksum mismatch, extract,
    # install) leaves nothing installed and returns non-zero.
    local tool="$1" url="$2" expected_sha="$3" dest="$4"
    local tmpdir tarball actual_sha rc=0
    tmpdir="$(mktemp -d)"
    tarball="$tmpdir/$tool.tar.gz"
    if ! curl -fsSL --retry 3 -o "$tarball" "$url"; then
        _log "$tool: download failed: $url"
        rm -rf "$tmpdir"
        return 1
    fi
    actual_sha="$(_sha256_of "$tarball")"
    if [ "$actual_sha" != "$expected_sha" ]; then
        _log "$tool: sha256 MISMATCH for $url (expected ${expected_sha}, got ${actual_sha}); refusing to install"
        rm -rf "$tmpdir"
        return 1
    fi
    if ! tar -xzf "$tarball" -C "$tmpdir" "$tool"; then
        _log "$tool: could not extract the '$tool' binary from the tarball"
        rc=1
    elif ! { mkdir -p "$(dirname "$dest")" && install -m 0755 "$tmpdir/$tool" "$dest"; }; then
        _log "$tool: failed to install binary to $dest"
        rc=1
    fi
    rm -rf "$tmpdir"
    return "$rc"
}

_install_scanner() {
    local tool="$1"
    if _scanner_at_pinned_version "$tool"; then
        _log "$tool: already installed at pinned version $(_scanner_pinned_version "$tool"), skipping"
        return 0
    fi
    local arch asset_and_sha asset sha url
    arch="$(uname -m)"
    if ! asset_and_sha="$(_scanner_asset_for_arch "$tool" "$arch")"; then
        _log "$tool: no pinned binary for architecture '${arch}'; not installed"
        return 1
    fi
    asset="${asset_and_sha% *}"
    sha="${asset_and_sha#* }"
    url="$(_scanner_release_url "$tool" "$asset")"
    _log "$tool: installing v$(_scanner_pinned_version "$tool") from ${url}"
    if _fetch_verify_install "$tool" "$url" "$sha" "$SECRET_SCANNER_INSTALL_DIR/$tool"; then
        _log "$tool: installed to $SECRET_SCANNER_INSTALL_DIR/$tool"
    else
        _log "$tool: install FAILED"
        return 1
    fi
}

main() {
    local tools=("$@") tool rc=0
    if [ "${#tools[@]}" -eq 0 ]; then
        tools=(betterleaks kingfisher)
    fi
    for tool in "${tools[@]}"; do
        case "$tool" in
            betterleaks | kingfisher) ;;
            *)
                _log "unknown scanner '${tool}' (expected betterleaks or kingfisher)"
                return 2
                ;;
        esac
    done
    # Installs stay independent: each runs regardless of the others' results.
    for tool in "${tools[@]}"; do
        _install_scanner "$tool" || rc=1
    done
    if [ "$rc" -eq 0 ]; then
        _log "all requested secret scanners are installed at their pinned versions"
    else
        _log "one or more secret-scanner installs failed; see logs above"
    fi
    return "$rc"
}

# Run main only when executed directly; unit tests source this file to
# exercise individual functions in isolation.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
