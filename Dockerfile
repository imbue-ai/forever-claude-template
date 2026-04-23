FROM python:3.12.13-slim

# Pinned versions for reproducible builds. Bump deliberately, not by accident.
ARG TTYD_VERSION=1.7.7
ARG CLOUDFLARED_VERSION=2026.3.0
ARG UV_VERSION=0.11.7
ARG CLAUDE_CODE_VERSION=2.1.116
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

# Install Node.js for building the minds-workspace-server frontend.
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
ARG LATCHKEY_VERSION=2.7.1
RUN npm install -g "latchkey@${LATCHKEY_VERSION}"

# install python dependencies
RUN uv tool install "modal==${MODAL_VERSION}"

# copy in all of our code:
COPY . /code/

# set working directory to the project root
WORKDIR /code/

# make an extra directory for future worktrees
RUN mkdir -p /worktree

# extract our code into the project directory
RUN git config --global --add safe.directory /code/ && chown -R root:root /code/

# Build the minds-workspace-server frontend
RUN cd /code/vendor/mngr/apps/minds_workspace_server/frontend && \
    npm ci && \
    npm run build

# add mngr and minds-workspace-server as tools (both need the plugin packages
# so they can parse plugin-specific config fields like auto_dismiss_dialogs)
RUN uv tool install -e /code/vendor/mngr/libs/mngr && \
    uv tool install -e /code/vendor/mngr/apps/minds_workspace_server \
        --with-editable /code/vendor/mngr/libs/mngr_claude \
        --with-editable /code/vendor/mngr/libs/mngr_modal && \
    mngr plugin add \
    --path vendor/mngr/libs/mngr_modal/ \
    --path vendor/mngr/libs/mngr_claude \
    --path vendor/mngr/libs/mngr_wait

# Run idly forever while being responsive to SIGTERM.
# PID 1 must explicitly install signal handlers in order to respect signals.
# `tail -f /dev/null` does not do this.
# Since `docker stop` issues a `SIGTERM`, we use an explicit `trap`.
# In practice, this appears to enable rapid interactions using `docker stop`.
CMD ["sh", "-c", "trap 'exit 0' TERM; tail -f /dev/null & wait"]
