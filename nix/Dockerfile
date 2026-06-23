FROM nixos/nix:2.34.7@sha256:bf1d938835ab96312f098fa6c2e9cab367728e0aad0646ee3e02a787c80d8fb8 AS fct-nix-base

SHELL ["/root/.nix-profile/bin/bash", "-euo", "pipefail", "-c"]

# /root/.local/bin holds uv + claude (installed by scripts/setup_system_nixos.sh);
# put it on PATH for every build layer and at runtime.
ENV PATH="/root/.local/bin:$PATH"

# Pin Claude Code; passed to setup_system_nixos.sh and recorded for the runtime
# version check. Keep in sync with agent_types.claude.version in
# .mngr/settings.toml and the default in scripts/setup_system_nixos.sh. Bump
# deliberately, not by accident.
ARG CLAUDE_CODE_VERSION=2.1.160
ENV CLAUDE_CODE_VERSION=${CLAUDE_CODE_VERSION}

# ============================================================================
# Nix system toolchain (repo-independent).
# ============================================================================
COPY scripts/setup_system_nixos.sh /usr/local/bin/fct-setup-system
COPY flake.nix flake.lock /tmp/fct-nix/
COPY nix/fct-workspace-env.nix /tmp/fct-nix/nix/fct-workspace-env.nix
COPY nix/fct-workspace-closure.*.txt /tmp/fct-nix/nix/
RUN chmod +x /usr/local/bin/fct-setup-system \
    && /usr/local/bin/fct-setup-system build-nix-profile

FROM fct-nix-base AS fct-nix-profile
RUN /usr/local/bin/fct-setup-system verify-nix-closure

FROM fct-nix-base AS fct-nix-profile-generate

FROM fct-nix-profile AS workspace

SHELL ["/root/.nix-profile/bin/bash", "-euo", "pipefail", "-c"]

# Compatibility paths expected by mngr's Docker/SSH provider and by downloaded
# tools that assume FHS dynamic-loader locations.
RUN /usr/local/bin/fct-setup-system setup-system

# Safety-net symlinks: /code -> /mngr/code and /worktree -> /mngr/worktree.
RUN ln -s /mngr/code /code && ln -s /mngr/worktree /worktree

# ============================================================================
# Pre-COPY manifest layer. Mirrors the existing Dockerfile so dependency work
# caches against manifests instead of application source.
# ============================================================================
WORKDIR /mngr/code/

COPY pyproject.toml uv.lock /mngr/code/
COPY libs/app_watcher/pyproject.toml /mngr/code/libs/app_watcher/pyproject.toml
COPY libs/bootstrap/pyproject.toml /mngr/code/libs/bootstrap/pyproject.toml
COPY libs/cloudflare_tunnel/pyproject.toml /mngr/code/libs/cloudflare_tunnel/pyproject.toml
COPY libs/runtime_backup/pyproject.toml /mngr/code/libs/runtime_backup/pyproject.toml
COPY libs/telegram_bot/pyproject.toml /mngr/code/libs/telegram_bot/pyproject.toml
COPY libs/web_server/pyproject.toml /mngr/code/libs/web_server/pyproject.toml
COPY apps/system_interface/pyproject.toml /mngr/code/apps/system_interface/pyproject.toml

COPY vendor/mngr/libs/imbue_common/pyproject.toml /mngr/code/vendor/mngr/libs/imbue_common/pyproject.toml
COPY vendor/mngr/libs/mngr/pyproject.toml /mngr/code/vendor/mngr/libs/mngr/pyproject.toml
COPY vendor/mngr/libs/mngr_claude/pyproject.toml /mngr/code/vendor/mngr/libs/mngr_claude/pyproject.toml
COPY vendor/mngr/libs/mngr_modal/pyproject.toml /mngr/code/vendor/mngr/libs/mngr_modal/pyproject.toml
COPY vendor/mngr/libs/mngr_wait/pyproject.toml /mngr/code/vendor/mngr/libs/mngr_wait/pyproject.toml
COPY vendor/mngr/libs/resource_guards/pyproject.toml /mngr/code/vendor/mngr/libs/resource_guards/pyproject.toml
COPY vendor/mngr/libs/concurrency_group/pyproject.toml /mngr/code/vendor/mngr/libs/concurrency_group/pyproject.toml

COPY apps/system_interface/frontend/package.json apps/system_interface/frontend/package-lock.json /mngr/code/apps/system_interface/frontend/

COPY scripts/install_dependencies.sh /usr/local/bin/fct-install-dependencies
RUN chmod +x /usr/local/bin/fct-install-dependencies && /usr/local/bin/fct-install-dependencies

# ============================================================================
# End pre-COPY manifest layer. Source-changing layers begin below.
# ============================================================================

COPY . /mngr/code/

RUN bash /mngr/code/scripts/build_workspace.sh

RUN mv /mngr/code /docker_build_code

COPY scripts/fct_seed.sh /usr/local/bin/fct-seed
RUN chmod +x /usr/local/bin/fct-seed
