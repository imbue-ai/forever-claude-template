FROM python:3.12.13-slim

# Pinned versions for reproducible builds. Bump deliberately, not by accident.
ARG TTYD_VERSION=1.7.7
ARG CLOUDFLARED_VERSION=2026.3.0
ARG UV_VERSION=0.11.7
ARG CLAUDE_CODE_VERSION=2.1.141
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
ARG LATCHKEY_VERSION=2.11.3
RUN npm install -g "latchkey@${LATCHKEY_VERSION}"

# install python dependencies
RUN uv tool install "modal==${MODAL_VERSION}"

# Playwright + Chromium is deliberately NOT installed here. The container
# starts and the bootstrap services come up without it; the
# `deferred-install` service (services.toml) installs it idempotently on
# first boot and writes a marker file so subsequent restarts no-op.

# ============================================================================
# Pre-COPY manifest layer.
# Copies only the dependency manifests (no application source) so the
# expensive `uv sync` and `npm ci` steps below cache against
# dependency-manifest changes only. Application code edits land on the
# `COPY . /code/` further down -- they do not invalidate the cache here.
# ============================================================================
WORKDIR /code/

# Root + per-workspace-member pyproject.toml + uv.lock.
COPY pyproject.toml uv.lock /code/
COPY libs/app_watcher/pyproject.toml /code/libs/app_watcher/pyproject.toml
COPY libs/bootstrap/pyproject.toml /code/libs/bootstrap/pyproject.toml
COPY libs/cloudflare_tunnel/pyproject.toml /code/libs/cloudflare_tunnel/pyproject.toml
COPY libs/runtime_backup/pyproject.toml /code/libs/runtime_backup/pyproject.toml
COPY libs/telegram_bot/pyproject.toml /code/libs/telegram_bot/pyproject.toml
COPY libs/web_server/pyproject.toml /code/libs/web_server/pyproject.toml
COPY apps/system_interface/pyproject.toml /code/apps/system_interface/pyproject.toml

# vendor/mngr path-dependency manifests. The root pyproject.toml's
# [tool.uv.sources] points imbue-common, imbue-mngr, imbue-mngr-claude,
# resource-guards, and concurrency-group at vendor/mngr/libs/<pkg>; uv
# needs each pyproject.toml present to resolve the workspace. mngr_modal
# and mngr_wait are also workspace members whose transitive deps benefit
# from pre-warming even though only mngr_wait is registered post-COPY
# (as a mngr plugin, not a tool install).
COPY vendor/mngr/libs/imbue_common/pyproject.toml /code/vendor/mngr/libs/imbue_common/pyproject.toml
COPY vendor/mngr/libs/mngr/pyproject.toml /code/vendor/mngr/libs/mngr/pyproject.toml
COPY vendor/mngr/libs/mngr_claude/pyproject.toml /code/vendor/mngr/libs/mngr_claude/pyproject.toml
COPY vendor/mngr/libs/mngr_modal/pyproject.toml /code/vendor/mngr/libs/mngr_modal/pyproject.toml
COPY vendor/mngr/libs/mngr_wait/pyproject.toml /code/vendor/mngr/libs/mngr_wait/pyproject.toml
COPY vendor/mngr/libs/resource_guards/pyproject.toml /code/vendor/mngr/libs/resource_guards/pyproject.toml
COPY vendor/mngr/libs/concurrency_group/pyproject.toml /code/vendor/mngr/libs/concurrency_group/pyproject.toml

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
COPY apps/system_interface/frontend/package.json apps/system_interface/frontend/package-lock.json /code/apps/system_interface/frontend/
RUN cd /code/apps/system_interface/frontend && npm ci

# ============================================================================
# End pre-COPY manifest layer. Source-changing layers begin below.
# ============================================================================

# copy in all of our code:
COPY . /code/

# make an extra directory for future worktrees
RUN mkdir -p /worktree

RUN ln -sf /code/vendor/tk/ticket /usr/local/bin/tk && \
    ln -sf /code/vendor/tk/ticket /usr/local/bin/ticket

# Mark /code/ as a git safe.directory so commands run inside the container
# don't refuse on ownership mismatch. No chown is needed: COPY already
# lands files as root:root by default.
RUN git config --global --add safe.directory /code/

# Build the system_interface frontend (deps already installed pre-COPY).
RUN cd /code/apps/system_interface/frontend && npm run build

# add mngr and system-interface as tools (both need the plugin packages
# so they can parse plugin-specific config fields like auto_dismiss_dialogs).
# mngr_modal is intentionally NOT installed/registered here because the FCT
# .mngr/settings.toml sets providers.modal.is_enabled = false; without it,
# `mngr plugin add` no longer has to inject a third plugin into the mngr
# tool venv, which is the dominant cost of this RUN.
RUN uv tool install -e /code/vendor/mngr/libs/mngr && \
    uv tool install -e /code/apps/system_interface \
        --with-editable /code/vendor/mngr/libs/mngr_claude && \
    mngr plugin add \
    --path vendor/mngr/libs/mngr_claude \
    --path vendor/mngr/libs/mngr_wait

# Sync the workspace venv. --frozen asserts the lockfile is canonical so
# the pre-warm cache layer is never bypassed by a silent re-resolve.
RUN uv sync --all-packages --frozen

# Run idly forever while being responsive to SIGTERM.
# PID 1 must explicitly install signal handlers in order to respect signals.
# `tail -f /dev/null` does not do this.
# Since `docker stop` issues a `SIGTERM`, we use an explicit `trap`.
# In practice, this appears to enable rapid interactions using `docker stop`.
CMD ["sh", "-c", "trap 'exit 0' TERM; tail -f /dev/null & wait"]
