FROM python:3.12.13-slim

# Pinned versions for reproducible builds. Bump deliberately, not by accident.
ARG TTYD_VERSION=1.7.7
ARG CLOUDFLARED_VERSION=2026.3.0
ARG UV_VERSION=0.11.7
ARG CLAUDE_CODE_VERSION=2.1.160
ARG MODAL_VERSION=1.4.2
ARG NODE_MAJOR=20

# Install system dependencies including tini for proper signal handling
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    build-essential \
    ca-certificates \
    curl \
    fd-find \
    git \
    git-lfs \
    jq \
    less \
    nano \
    openssh-server \
    procps \
    restic \
    ripgrep \
    rsync \
    sqlite3 \
    tini \
    tmux \
    unison \
    wget \
    xxd \
    xmlstarlet \
    && rm -rf /var/lib/apt/lists/*

# Install ttyd binary from GitHub releases (not available via apt)
RUN ARCH=$(uname -m) && \
    curl -fsSL "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/ttyd.${ARCH}" \
    -o /usr/local/bin/ttyd && \
    chmod +x /usr/local/bin/ttyd

# Install cloudflared for Cloudflare tunnel support
RUN ARCH=$(dpkg --print-architecture) && \
    curl -fsSL "https://github.com/cloudflare/cloudflared/releases/download/${CLOUDFLARED_VERSION}/cloudflared-linux-${ARCH}" \
    -o /usr/local/bin/cloudflared && \
    chmod +x /usr/local/bin/cloudflared

RUN mkdir -p -m 755 /etc/apt/keyrings \
	&& out=$(mktemp) && wget -nv -O$out https://cli.github.com/packages/githubcli-archive-keyring.gpg \
	&& cat $out | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null \
	&& chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
	&& mkdir -p -m 755 /etc/apt/sources.list.d \
	&& echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
	&& apt update \
	&& apt install gh -y

# Install uv (pinned to UV_VERSION; astral.sh serves versioned install scripts)
RUN curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" | sh && echo 'PATH="/root/.local/bin:$PATH"' >> /root/.bashrc
ENV PATH="/root/.local/bin:$PATH"

# Source /mngr/env (when present) for interactive bash sessions. The env file
# is mounted into the container at runtime and holds the variables mngr
# commands need; sourcing it here lets terminals spawned via
# `docker exec -it <container> bash` run mngr commands without manual setup.
RUN printf '%s\n' 'if [ -f /mngr/env ]; then set -a; . /mngr/env; set +a; fi' >> /root/.bashrc

# Install claude code (pinned via CLAUDE_CODE_VERSION build arg; bump in sync with
# agent_types.claude.version in .mngr/settings.toml so the provisioning-time
# version check matches)
RUN curl -fsSL https://claude.ai/install.sh > /tmp/install_claude.sh && \
    bash /tmp/install_claude.sh "$CLAUDE_CODE_VERSION" && \
    test -x /root/.local/bin/claude
ENV CLAUDE_CODE_VERSION=${CLAUDE_CODE_VERSION}

# Install Node.js for building the system_interface frontend.
# NodeSource's setup_${NODE_MAJOR}.x pins the major, apt resolves within that
# major. For full determinism we could fetch a static nodejs tarball instead;
# not doing so keeps the image size and setup simpler.
RUN curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

# Pre-seed github.com SSH host key so git operations don't block on
# interactive host-key confirmation (e.g. when Claude Code installs
# plugins from github:<owner>/<repo>).
RUN mkdir -p /root/.ssh && \
    chmod 700 /root/.ssh && \
    ssh-keyscan -t rsa,ecdsa,ed25519 github.com >> /root/.ssh/known_hosts && \
    chmod 600 /root/.ssh/known_hosts

# Install latchkey (CLI for making authenticated HTTP calls to third-party
# services). The agent runs it in gateway mode -- the per-agent
# LATCHKEY_GATEWAY URL is injected at `mngr create` time by the outside
# caller (see .mngr/settings.toml's pass_env), so we do not hardcode it here.
#
ARG LATCHKEY_VERSION=2.14.0
RUN npm install -g "latchkey@${LATCHKEY_VERSION}"

# install python dependencies
RUN uv tool install "modal==${MODAL_VERSION}"

# Playwright + Chromium is deliberately NOT installed here. The container
# starts and the bootstrap services come up without it; the
# `deferred-install` service (services.toml) installs it idempotently on
# first boot and writes a marker file so subsequent restarts no-op.

# Safety-net symlinks: /code -> /mngr/code and /worktree -> /mngr/worktree.
# All FCT-owned paths are written as /mngr/code/... and /mngr/worktree/...
# (so the workspace and worktrees ride the /mngr/ persistent volume for
# backup snapshots), but anything that straggled with a hard-coded /code/...
# or /worktree/... reference still resolves through these symlinks. The
# targets do not need to exist yet -- the WORKDIR + COPY layers below create
# /mngr/code/, and the first-boot CMD seeds the volume from /docker_build_code
# and `mkdir -p /mngr/worktree` so both symlinks resolve at runtime.
RUN ln -s /mngr/code /code && ln -s /mngr/worktree /worktree

# ============================================================================
# Pre-COPY manifest layer.
# Copies only the dependency manifests (no application source) so the
# expensive `uv sync` and `npm ci` steps below cache against
# dependency-manifest changes only. Application code edits land on the
# `COPY . /mngr/code/` further down -- they do not invalidate the cache here.
# ============================================================================
WORKDIR /mngr/code/

# Root + per-workspace-member pyproject.toml + uv.lock.
COPY pyproject.toml uv.lock /mngr/code/
COPY libs/app_watcher/pyproject.toml /mngr/code/libs/app_watcher/pyproject.toml
COPY libs/bootstrap/pyproject.toml /mngr/code/libs/bootstrap/pyproject.toml
COPY libs/cloudflare_tunnel/pyproject.toml /mngr/code/libs/cloudflare_tunnel/pyproject.toml
COPY libs/runtime_backup/pyproject.toml /mngr/code/libs/runtime_backup/pyproject.toml
COPY libs/telegram_bot/pyproject.toml /mngr/code/libs/telegram_bot/pyproject.toml
COPY libs/web_server/pyproject.toml /mngr/code/libs/web_server/pyproject.toml
COPY apps/system_interface/pyproject.toml /mngr/code/apps/system_interface/pyproject.toml

# vendor/mngr path-dependency manifests. The root pyproject.toml's
# [tool.uv.sources] points imbue-common, imbue-mngr, imbue-mngr-claude,
# resource-guards, and concurrency-group at vendor/mngr/libs/<pkg>; uv
# needs each pyproject.toml present to resolve the workspace. mngr_modal
# and mngr_wait are also workspace members whose transitive deps benefit
# from pre-warming even though only mngr_wait is registered post-COPY
# (as a mngr plugin, not a tool install).
COPY vendor/mngr/libs/imbue_common/pyproject.toml /mngr/code/vendor/mngr/libs/imbue_common/pyproject.toml
COPY vendor/mngr/libs/mngr/pyproject.toml /mngr/code/vendor/mngr/libs/mngr/pyproject.toml
COPY vendor/mngr/libs/mngr_claude/pyproject.toml /mngr/code/vendor/mngr/libs/mngr_claude/pyproject.toml
COPY vendor/mngr/libs/mngr_modal/pyproject.toml /mngr/code/vendor/mngr/libs/mngr_modal/pyproject.toml
COPY vendor/mngr/libs/mngr_wait/pyproject.toml /mngr/code/vendor/mngr/libs/mngr_wait/pyproject.toml
COPY vendor/mngr/libs/resource_guards/pyproject.toml /mngr/code/vendor/mngr/libs/resource_guards/pyproject.toml
COPY vendor/mngr/libs/concurrency_group/pyproject.toml /mngr/code/vendor/mngr/libs/concurrency_group/pyproject.toml

# Pre-warm the uv wheel cache. --no-install-workspace skips every libs/*
# and apps/system_interface (their source isn't here yet); --no-install-local
# skips the vendor/mngr path deps (same reason). What lands in the cache is
# every third-party PyPI dep in the lockfile, so the post-COPY
# `uv sync --all-packages --frozen` only has to register the editable
# workspace + path-dep packages -- no wheel downloads -- when application
# or vendor/mngr source changes invalidate the layers below.
RUN uv sync --all-packages --frozen --no-install-workspace --no-install-local

# Frontend npm dependencies. Same shape: copy only the lockfile + manifest,
# install, then `npm run build` post-COPY when the actual source is present.
COPY apps/system_interface/frontend/package.json apps/system_interface/frontend/package-lock.json /mngr/code/apps/system_interface/frontend/
RUN cd /mngr/code/apps/system_interface/frontend && npm ci

# ============================================================================
# End pre-COPY manifest layer. Source-changing layers begin below.
# ============================================================================

# copy in all of our code:
COPY . /mngr/code/

# Mark /mngr/code/ as a git safe.directory so commands run inside the container
# don't refuse on ownership mismatch. No chown is needed: COPY already
# lands files as root:root by default.
RUN git config --global --add safe.directory /mngr/code/

# Build the system_interface frontend (deps already installed pre-COPY).
RUN cd /mngr/code/apps/system_interface/frontend && npm run build

# add mngr and system-interface as tools (both need the plugin packages
# so they can parse plugin-specific config fields like auto_dismiss_dialogs).
# mngr_modal is intentionally NOT installed/registered here because the FCT
# .mngr/settings.toml sets providers.modal.is_enabled = false; without it,
# `mngr plugin add` no longer has to inject a third plugin into the mngr
# tool venv, which is the dominant cost of this RUN.
#
# UV_PYTHON pins uv to the container's already-installed python:3.12.13-slim
# interpreter at /usr/local/bin/python3.12, instead of letting uv download
# its own managed cpython (currently 3.14.x). The python-build-standalone
# cpython 3.14.x for linux-aarch64 SIGILLs at startup when this RUN executes
# under qemu-vz on Apple Silicon (verified on the mac launch-to-msg runner:
# run 27232809809, exit code 132 immediately after `Installed 1 executable:
# system-interface`, before `mngr plugin add` could even start). Pinning to
# the slim base's bundled 3.12.13 sidesteps the issue without changing the
# Python version mngr/system_interface get at runtime (they were resolving
# to the same minor anyway via uv's default version selection).
ENV UV_PYTHON=/usr/local/bin/python3.12
ENV UV_PYTHON_DOWNLOADS=never
# Install mngr + system_interface as separate RUN-pipeline stages with a
# `mngr --version` warmup in between. The previous form chained both
# installs and `mngr plugin add` in one `&&` chain, which reliably SIGILLed
# at ~3.4s into the RUN on Apple Silicon Lima Debian 12 ARM64 (verified
# 27234056261 with exit 132 immediately after "Installed 1 executable:
# system-interface"). Splitting -- and adding a warmup invocation of the
# freshly-installed mngr binary before plugin add -- empirically clears
# the SIGILL (verified 27234439518 reached creation status DONE). The
# precise root cause is suspected to be a JIT/import-cache race in the
# python-build-standalone cpython binaries combined with concurrent uv
# tool install activity; the warmup primes whatever needs priming.
RUN uv tool install -e /mngr/code/vendor/mngr/libs/mngr
RUN uv tool install -e /mngr/code/apps/system_interface \
        --with-editable /mngr/code/vendor/mngr/libs/mngr_claude
# Diagnostic: identify which package import SIGILLs. If `import imbue.mngr`
# crashes, the offender is in mngr's transitive dep closure. Print
# importlib metadata for the top-level wheels so we can correlate.
RUN python3.12 -c "import sys; print('python:', sys.version)" && \
    python3.12 -c "import importlib.metadata as m; ds=sorted({d.name for d in m.distributions()}); print('pkgs:', ds[:60])" && \
    /usr/local/bin/python3.12 -c "import click; print('click OK')" && \
    /usr/local/bin/python3.12 -c "import pydantic; print('pydantic OK')" && \
    /usr/local/bin/python3.12 -c "import pydantic_core; print('pydantic_core OK')" && \
    /usr/local/bin/python3.12 -c "import cryptography; print('cryptography OK')" && \
    /usr/local/bin/python3.12 -c "import modal; print('modal OK')" && \
    /usr/local/bin/python3.12 -c "import imbue.imbue_common; print('imbue.imbue_common OK')" && \
    /usr/local/bin/python3.12 -c "import imbue.mngr; print('imbue.mngr OK')"
RUN mngr plugin add \
    --path vendor/mngr/libs/mngr_claude \
    --path vendor/mngr/libs/mngr_wait

# Sync the workspace venv. --frozen asserts the lockfile is canonical so
# the pre-warm cache layer is never bypassed by a silent re-resolve.
RUN uv sync --all-packages --frozen

# Expose the vendored tk ticket tracker on PATH. `tk` is a portable bash
# script at vendor/tk/ticket; symlinks into /usr/local/bin/ (already on PATH)
# make both `tk` and `ticket` invocable without bundling an additional
# install mechanism. The target resolves once /mngr/code is seeded onto the
# runtime volume (see the bake-and-relocate dance below).
RUN ln -sf /mngr/code/vendor/tk/ticket /usr/local/bin/tk && \
    ln -sf /mngr/code/vendor/tk/ticket /usr/local/bin/ticket

# Move the baked workspace off the volume mount path so the shipped
# image has /mngr/code/ EMPTY. At runtime, /mngr/ is a persistent
# volume mount; any image-layer content sitting at /mngr/code/ would
# be shadowed by the mount. /docker_build_code holds the workspace
# until first boot, where the post-host-create seed step (see below)
# atomically relocates it onto the volume.
RUN mv /mngr/code /docker_build_code

# Install the first-boot seed script at a stable image-layer path. It
# has to live OUTSIDE /mngr/ (the volume mount path) so the runtime
# mount does not shadow it, and OUTSIDE /docker_build_code (which the
# seed itself cleans up after relocating). /usr/local/bin/ is on PATH,
# is image-layer, and survives the bake-and-relocate dance.
#
# mngr invokes this script synchronously via the `post_host_create_command`
# create-template hook (see .mngr/settings.toml) after the host is online
# but before any agent work_dir setup -- the seed therefore has the
# volume mount, the /mngr symlink dance, and sshd all in place, and
# completes before anything else writes to /mngr/code.
#
# Source mode bits are already +x; chmod is defensive in case the file
# is checked out without exec bits.
COPY scripts/fct_seed.sh /usr/local/bin/fct-seed
RUN chmod +x /usr/local/bin/fct-seed
