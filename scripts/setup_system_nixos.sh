#!/usr/bin/env bash
# NixOS-backed system-toolchain setup for nix/Dockerfile.
#
# Mirrors scripts/setup_system.sh at the Dockerfile level while keeping the
# Nix-specific profile build, closure verification, and FHS compatibility shims
# in one script.
set -euo pipefail

: "${FCT_NIX_PROFILE:=/nix/var/nix/profiles/fct-workspace}"
: "${UV_VERSION:=0.11.7}"
: "${CLAUDE_CODE_VERSION:=2.1.160}"
: "${MODAL_VERSION:=1.4.2}"
: "${LATCHKEY_VERSION:=2.17.1}"

export FCT_NIX_PROFILE
export NPM_CONFIG_PREFIX="${NPM_CONFIG_PREFIX:-/usr/local}"
export OPENSSL_armcap="${OPENSSL_armcap:-0}"

_export_nix_env() {
    export PATH="/root/.local/bin:/usr/local/bin:${FCT_NIX_PROFILE}/bin:${FCT_NIX_PROFILE}/sbin:/root/.nix-profile/bin:/nix/var/nix/profiles/default/bin:/nix/var/nix/profiles/default/sbin:$PATH"
    export LD_LIBRARY_PATH="${FCT_NIX_PROFILE}/lib:${FCT_NIX_PROFILE}/lib64:/usr/local/lib:/usr/lib:${LD_LIBRARY_PATH:-}"
}

_nix_system() {
    case "$(uname -m)" in
        x86_64) printf '%s\n' x86_64-linux ;;
        aarch64 | arm64) printf '%s\n' aarch64-linux ;;
        *) echo "unsupported docker architecture: $(uname -m)" >&2; exit 1 ;;
    esac
}

build_nix_profile() {
    _export_nix_env
    nix --extra-experimental-features "nix-command flakes" build \
        --no-update-lock-file \
        --out-link "$FCT_NIX_PROFILE" \
        /tmp/fct-nix#fct-workspace-env

    mkdir -p /etc/fct-workspace
    nix-store -q --requisites "$FCT_NIX_PROFILE" | sort > /etc/fct-workspace/nix-closure.txt
}

verify_nix_closure() {
    _export_nix_env
    expected_manifest="/tmp/fct-nix/nix/fct-workspace-closure.$(_nix_system).txt"
    test -f "$expected_manifest"
    diff -u "$expected_manifest" /etc/fct-workspace/nix-closure.txt
}

setup_compatibility_paths() {
    _export_nix_env
    mkdir -p /etc/fonts /etc/ssl/certs /etc/ssh /run/sshd /usr/lib /usr/sbin /usr/local/bin /var/empty/sshd
    ln -sfn /run /var/run

    grep -q '^sshd:' /etc/group || echo 'sshd:x:74:' >> /etc/group
    grep -q '^sshd:' /etc/passwd || echo 'sshd:x:74:74:Privilege-separated SSH:/var/empty/sshd:/bin/sh' >> /etc/passwd
    sed -i -E 's/^root:!+:/root:*:/' /etc/shadow

    sshd_realpath="$(readlink -f "$(command -v sshd)")"
    sftp_server="$(dirname "$(dirname "$sshd_realpath")")/libexec/sftp-server"
    test -x "$sftp_server"
    ln -sf "$sshd_realpath" /usr/sbin/sshd
    ln -sf "$sftp_server" /usr/local/bin/sftp-server

    ln -sf "$FCT_NIX_PROFILE/lib/libstdc++.so.6" /usr/lib/libstdc++.so.6
    ln -sf "$FCT_NIX_PROFILE/lib/libgcc_s.so.1" /usr/lib/libgcc_s.so.1

    for browser_lib in \
        libglib-2.0.so.0 \
        libgobject-2.0.so.0 \
        libnspr4.so \
        libnss3.so \
        libnssutil3.so \
        libgio-2.0.so.0 \
        libatk-1.0.so.0 \
        libdbus-1.so.3 \
        libexpat.so.1 \
        libatspi.so.0 \
        libX11.so.6 \
        libXcomposite.so.1 \
        libXdamage.so.1 \
        libXext.so.6 \
        libXfixes.so.3 \
        libXrandr.so.2 \
        libgbm.so.1 \
        libxcb.so.1 \
        libxkbcommon.so.0 \
        libudev.so.1 \
        libasound.so.2 \
        libcups.so.2 \
        libcairo.so.2 \
        libpango-1.0.so.0; do
        browser_lib_path="$(find /nix/store -path "*/lib/$browser_lib" -print -quit)"
        test -n "$browser_lib_path"
        ln -sf "$browser_lib_path" "/usr/lib/$browser_lib"
    done

    cert_bundle="$FCT_NIX_PROFILE/etc/ssl/certs/ca-bundle.crt"
    test -f "$cert_bundle"
    ln -sf "$cert_bundle" /etc/ssl/certs/ca-certificates.crt

    fontconfig_conf="$(find /nix/store -path '*/etc/fonts/fonts.conf' -print -quit)"
    test -f "$fontconfig_conf"
    ln -sf "$fontconfig_conf" /etc/fonts/fonts.conf

    printf '%s\n' \
        '#!/usr/bin/env bash' \
        'set -euo pipefail' \
        'test -f /etc/ssl/certs/ca-certificates.crt' \
        > /usr/local/bin/update-ca-certificates
    chmod +x /usr/local/bin/update-ca-certificates

    dynamic_linker="$(nix-instantiate --eval --strict -E '(import <nixpkgs> {}).stdenv.cc.bintools.dynamicLinker' | tr -d '"')"
    case "$(uname -m)" in
        x86_64) mkdir -p /lib64; ln -sf "$dynamic_linker" /lib64/ld-linux-x86-64.so.2 ;;
        aarch64 | arm64) mkdir -p /lib; ln -sf "$dynamic_linker" /lib/ld-linux-aarch64.so.1 ;;
        *) echo "unsupported docker architecture: $(uname -m)" >&2; exit 1 ;;
    esac

    ssh-keygen -A
    {
        echo 'PermitRootLogin yes'
        echo 'PubkeyAuthentication yes'
        echo 'PasswordAuthentication no'
        echo 'KbdInteractiveAuthentication no'
        echo 'AuthorizedKeysFile .ssh/authorized_keys'
        echo 'PidFile /run/sshd/sshd.pid'
        echo "SetEnv FCT_NIX_PROFILE=$FCT_NIX_PROFILE PATH=/root/.local/bin:/usr/local/bin:$FCT_NIX_PROFILE/bin:$FCT_NIX_PROFILE/sbin:/usr/bin:/bin LD_LIBRARY_PATH=$FCT_NIX_PROFILE/lib:$FCT_NIX_PROFILE/lib64:/usr/local/lib:/usr/lib OPENSSL_armcap=0"
        echo "Subsystem sftp $sftp_server"
    } > /etc/ssh/sshd_config
    /usr/sbin/sshd -t
}

expose_provider_bootstrap_commands() {
    _export_nix_env
    mkdir -p /root/.local/bin

    for command_name in git curl tmux rsync jq xxd flock; do
        command_path="$(command -v "$command_name")"
        test -n "$command_path"
        ln -sf "$command_path" "/root/.local/bin/$command_name"
    done
}

install_uv_and_claude() {
    _export_nix_env
    curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" | sh
    _export_nix_env
    curl -fsSL https://claude.ai/install.sh > /tmp/install_claude.sh
    bash /tmp/install_claude.sh "${CLAUDE_CODE_VERSION}"
    test -x /root/.local/bin/claude
}

write_shell_profile() {
    mkdir -p /etc/profile.d
    printf '%s\n' \
        "export FCT_NIX_PROFILE=\"${FCT_NIX_PROFILE}\"" \
        "export PATH=\"/root/.local/bin:/usr/local/bin:${FCT_NIX_PROFILE}/bin:${FCT_NIX_PROFILE}/sbin:\$PATH\"" \
        "export LD_LIBRARY_PATH=\"${FCT_NIX_PROFILE}/lib:${FCT_NIX_PROFILE}/lib64:/usr/local/lib:/usr/lib:\${LD_LIBRARY_PATH:-}\"" \
        > /etc/profile.d/fct_path.sh

    if ! grep -q 'FCT_NIX_PROFILE' /root/.bashrc 2>/dev/null; then
        echo 'export FCT_NIX_PROFILE="'"$FCT_NIX_PROFILE"'"' >> /root/.bashrc
        echo 'PATH="/root/.local/bin:/usr/local/bin:'"$FCT_NIX_PROFILE"'/bin:'"$FCT_NIX_PROFILE"'/sbin:$PATH"' >> /root/.bashrc
        echo 'export LD_LIBRARY_PATH="'"$FCT_NIX_PROFILE"'/lib:'"$FCT_NIX_PROFILE"'/lib64:/usr/local/lib:/usr/lib:${LD_LIBRARY_PATH:-}"' >> /root/.bashrc
    fi
    if ! grep -q '/mngr/env' /root/.bashrc 2>/dev/null; then
        printf '%s\n' 'if [ -f /mngr/env ]; then set -a; . /mngr/env; set +a; fi' >> /root/.bashrc
    fi
}

seed_github_host_keys() {
    mkdir -p /root/.ssh
    chmod 700 /root/.ssh
    if ! grep -q "github.com" /root/.ssh/known_hosts 2>/dev/null; then
        ssh-keyscan -t rsa,ecdsa,ed25519 github.com >> /root/.ssh/known_hosts
    fi
    chmod 600 /root/.ssh/known_hosts
}

install_global_tools() {
    _export_nix_env
    npm install -g "latchkey@${LATCHKEY_VERSION}"
    uv tool install "modal==${MODAL_VERSION}"
}

setup_system() {
    setup_compatibility_paths
    expose_provider_bootstrap_commands
    install_uv_and_claude
    write_shell_profile
    seed_github_host_keys
    install_global_tools
}

case "${1:-}" in
    build-nix-profile) build_nix_profile ;;
    verify-nix-closure) verify_nix_closure ;;
    setup-system) setup_system ;;
    *) echo "usage: $0 {build-nix-profile|verify-nix-closure|setup-system}" >&2; exit 2 ;;
esac
