#!/usr/bin/env bash
# Shared system-toolchain setup for default-workspace-template hosts.
#
# Installs the repo-independent toolchain: system packages, language runtimes,
# and pinned CLIs. This is the single source of truth for that setup -- the
# Dockerfile RUNs it (docker / vps_docker / ovh providers) and the Lima provider
# runs it directly in the VM as root. It needs no repo content, must run as root,
# and is idempotent so re-running is safe.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# Skip if this exact repo tree was already provisioned (e.g. baked into the image).
. "$(dirname "$0")/_provision_guard.sh"
provision_skip_if_done setup_system

# Pinned versions (single source of truth; override via env if needed). Keep
# CLAUDE_CODE_VERSION in sync with agent_types.claude.version in .mngr/settings.toml.
: "${TTYD_VERSION:=1.7.7}"
: "${CLOUDFLARED_VERSION:=2026.3.0}"
: "${UV_VERSION:=0.11.7}"
: "${CLAUDE_CODE_VERSION:=2.1.207}"
: "${MODAL_VERSION:=1.4.2}"
: "${NODE_MAJOR:=20}"
: "${LATCHKEY_VERSION:=2.21.0}"
: "${RESTIC_VERSION:=0.18.1}"

# System packages (tini for signal handling; supervisor runs our background
# services; earlyoom is the OOM-prevention daemon that sheds memory under
# pressure before the kernel kills an arbitrary victim; the rest are
# agent/runtime deps). supervisor provides the system supervisord + supervisorctl
# that `uv run bootstrap` execs into the foreground.
apt-get update
apt-get install -y --no-install-recommends \
    bash build-essential ca-certificates curl earlyoom fd-find git git-lfs jq less nano \
    openssh-server procps restic ripgrep rsync sqlite3 supervisor tini tmux unison util-linux wget \
    xxd xmlstarlet
rm -rf /var/lib/apt/lists/*

# The Debian `supervisor` package enables a systemd unit that immediately starts
# a supervisord against the default /etc/supervisor/supervisord.conf. On
# systemd-based providers (lima/VPS) that daemon grabs /var/run/supervisor.sock
# and makes `uv run bootstrap`'s `supervisord -c /mngr/code/supervisord.conf`
# fail with "Another program is already listening". We always launch our own
# supervisord from bootstrap, so disable + mask the packaged unit. Guarded so
# it is a no-op on docker (no systemd / no systemctl on the slim image).
if command -v systemctl >/dev/null 2>&1; then
    systemctl disable --now supervisor 2>/dev/null || true
    systemctl mask supervisor 2>/dev/null || true
fi

# The distro restic (bookworm ships 0.14) predates `restic restore --delete`,
# which the minds in-place backup restore requires (restic >= 0.17). Install
# the pinned release (sha256-verified, from the official SHA256SUMS) at
# /usr/local/bin so it shadows the apt binary and the whole workspace --
# including the hourly host-backup service -- runs the same pinned version
# minds bundles on the desktop side. The apt package above stays as a
# fallback for anything resolving /usr/bin/restic explicitly.
restic_arch="$(uname -m)"
case "${restic_arch}" in
    x86_64) restic_goarch="amd64"; restic_sha256="680838f19d67151adba227e1570cdd8af12c19cf1735783ed1ba928bc41f363d" ;;
    aarch64) restic_goarch="arm64"; restic_sha256="87f53fddde38764095e9c058a3b31834052c37e5826d2acf34e18923c006bd45" ;;
    *) echo "Unsupported architecture for restic: ${restic_arch}" >&2; exit 1 ;;
esac
curl -fsSL "https://github.com/restic/restic/releases/download/v${RESTIC_VERSION}/restic_${RESTIC_VERSION}_linux_${restic_goarch}.bz2" -o /tmp/restic.bz2
echo "${restic_sha256}  /tmp/restic.bz2" | sha256sum -c -
bunzip2 -c /tmp/restic.bz2 > /usr/local/bin/restic
chmod +x /usr/local/bin/restic
rm /tmp/restic.bz2

# ttyd (terminal-over-web) binary from GitHub releases (not in apt).
ttyd_arch="$(uname -m)"
curl -fsSL "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/ttyd.${ttyd_arch}" -o /usr/local/bin/ttyd
chmod +x /usr/local/bin/ttyd

# cloudflared for Cloudflare tunnel support.
cloudflared_arch="$(dpkg --print-architecture)"
curl -fsSL "https://github.com/cloudflare/cloudflared/releases/download/${CLOUDFLARED_VERSION}/cloudflared-linux-${cloudflared_arch}" -o /usr/local/bin/cloudflared
chmod +x /usr/local/bin/cloudflared

# GitHub CLI from its official apt repo.
mkdir -p -m 755 /etc/apt/keyrings
gh_keyring="$(mktemp)"
wget -nv -O"$gh_keyring" https://cli.github.com/packages/githubcli-archive-keyring.gpg
tee /etc/apt/keyrings/githubcli-archive-keyring.gpg < "$gh_keyring" > /dev/null
chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
mkdir -p -m 755 /etc/apt/sources.list.d
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list
apt-get update
apt-get install -y gh
rm -rf /var/lib/apt/lists/*

# uv (pinned). Installs to /root/.local/bin.
curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" | sh
export PATH="/root/.local/bin:$PATH"

# Ensure a uv-managed Python that satisfies the workspace lockfile (>=3.12).
# The Docker base image ships 3.12, but other bases (e.g. a Debian VM whose
# system Python is 3.11) do not -- and the root pyproject's requires-python
# (>=3.11) lets uv otherwise pick the system 3.11, which the frozen lock then
# rejects. Fetch a managed 3.12 here so install_dependencies.sh /
# build_workspace.sh can pin uv to it. No-op when system Python is already
# >=3.12, so the Docker build is unchanged.
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
    uv python install 3.12
fi

# Make /root/.local/bin discoverable in login and interactive shells. The docker
# image also sets ENV PATH; the Lima VM relies on these profile writes.
if ! grep -q '/root/.local/bin' /root/.bashrc 2>/dev/null; then
    echo 'PATH="/root/.local/bin:$PATH"' >> /root/.bashrc
fi
printf '%s\n' 'PATH="/root/.local/bin:$PATH"' > /etc/profile.d/default_workspace_template_path.sh

# Source /mngr/env (when present) for interactive bash sessions so terminals can
# run mngr commands without manual setup.
if ! grep -q '/mngr/env' /root/.bashrc 2>/dev/null; then
    printf '%s\n' 'if [ -f /mngr/env ]; then set -a; . /mngr/env; set +a; fi' >> /root/.bashrc
fi

# Claude Code CLI (pinned; the provisioning-time version check expects this exact version).
curl -fsSL https://claude.ai/install.sh > /tmp/install_claude.sh
bash /tmp/install_claude.sh "${CLAUDE_CODE_VERSION}"
test -x /root/.local/bin/claude

# Node.js (NodeSource pins the major; apt resolves within it).
curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -
apt-get install -y nodejs
rm -rf /var/lib/apt/lists/*

# Pre-seed github.com SSH host keys so git operations don't block on interactive
# host-key confirmation. Idempotent: only added when absent.
mkdir -p /root/.ssh
chmod 700 /root/.ssh
if ! grep -q "github.com" /root/.ssh/known_hosts 2>/dev/null; then
    ssh-keyscan -t rsa,ecdsa,ed25519 github.com >> /root/.ssh/known_hosts
fi
chmod 600 /root/.ssh/known_hosts

# latchkey (gateway CLI) and modal (python tool).
npm install -g "latchkey@${LATCHKEY_VERSION}"
uv tool install "modal==${MODAL_VERSION}"

# Secret-scanner binaries (betterleaks + kingfisher) for the publish-inspiration
# scan gate. install_secret_scanners.sh is the single source of truth for the
# version pins + per-arch sha256s; invoking it here means BOTH docker-built
# images (this script runs in a Dockerfile RUN) and Lima-provisioned VMs (this
# script runs directly in the VM) bake in the scanners from one common place.
# The installer is reachable two ways depending on how we were invoked: in a
# Dockerfile build it sits beside this script's install path as
# default-workspace-template-install-secret-scanners; run straight from the repo (Lima/Modal)
# it is its sibling install_secret_scanners.sh. It is idempotent (skips any tool
# already at its pinned version without network access).
setup_dir="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$setup_dir/install_secret_scanners.sh" ]; then
    bash "$setup_dir/install_secret_scanners.sh"
else
    bash "$setup_dir/default-workspace-template-install-secret-scanners"
fi

# Playwright + Chromium is deliberately NOT installed here; the deferred-install
# service installs it idempotently on first boot.

provision_mark_done setup_system
