FROM python:3.12.13-slim-bookworm

# /root/.local/bin holds uv + claude (installed by scripts/setup_system.sh); put
# it on PATH for every build layer and at runtime.
ENV PATH="/root/.local/bin:$PATH"

# Pin Claude Code; passed to setup_system.sh and recorded for the runtime version
# check. Keep in sync with agent_types.claude.version in .mngr/settings.toml and
# the default in scripts/setup_system.sh. Bump deliberately, not by accident.
ARG CLAUDE_CODE_VERSION=2.1.160
ENV CLAUDE_CODE_VERSION=${CLAUDE_CODE_VERSION}

# ============================================================================
# System toolchain (repo-independent). Shared verbatim with the Lima provider,
# which runs this exact script in the VM. Copied with its sibling
# _provision_guard.sh (which setup_system.sh sources via `dirname "$0"`) and
# nothing else, so this expensive, stable layer caches against the scripts +
# pinned versions, not application source.
# ============================================================================
COPY scripts/setup_system.sh /usr/local/bin/default-workspace-template-setup-system
COPY scripts/_provision_guard.sh /usr/local/bin/_provision_guard.sh
RUN chmod +x /usr/local/bin/default-workspace-template-setup-system && default-workspace-template-setup-system

# ============================================================================
# Secret-scanner binaries (repo-independent, pinned). Baked at image-build
# time so the publish-inspiration skill's scan gate (scan_secrets.sh) can
# hard-require betterleaks + trufflehog + kingfisher from the first second of
# every docker-built container. The script is the single source of truth for
# the version pins and per-arch sha256s and skips any scanner already present
# at its pinned version. If a binary is ever missing (an environment not built
# from this Dockerfile, or a failed bake), the script is runnable by hand to
# install all three -- the scan gate's error names that command.
# Kept above the `COPY . /mngr/code/` layer so it caches against the script
# content (the pins live inside it), not application source.
# ============================================================================
COPY scripts/install_secret_scanners.sh /usr/local/bin/default-workspace-template-install-secret-scanners
RUN chmod +x /usr/local/bin/default-workspace-template-install-secret-scanners && default-workspace-template-install-secret-scanners

# Safety-net symlinks: /code -> /mngr/code and /worktree -> /mngr/worktree.
# All default-workspace-template-owned paths are written as /mngr/code/... and /mngr/worktree/...
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
# expensive dependency install below caches against dependency-manifest
# changes only. Application code edits land on the `COPY . /mngr/code/`
# further down -- they do not invalidate the cache here.
# ============================================================================
WORKDIR /mngr/code/

# Root + per-workspace-member pyproject.toml + uv.lock.
COPY pyproject.toml uv.lock /mngr/code/
COPY libs/app_watcher/pyproject.toml /mngr/code/libs/app_watcher/pyproject.toml
COPY libs/bootstrap/pyproject.toml /mngr/code/libs/bootstrap/pyproject.toml
COPY libs/cloudflare_tunnel/pyproject.toml /mngr/code/libs/cloudflare_tunnel/pyproject.toml
COPY libs/runtime_backup/pyproject.toml /mngr/code/libs/runtime_backup/pyproject.toml
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

# Frontend npm manifest (lockfile + package.json) -- install needs only these.
COPY apps/system_interface/frontend/package.json apps/system_interface/frontend/package-lock.json /mngr/code/apps/system_interface/frontend/

# Dependency install (manifests only). Shared verbatim with the Lima provider.
COPY scripts/install_dependencies.sh /usr/local/bin/default-workspace-template-install-dependencies
RUN chmod +x /usr/local/bin/default-workspace-template-install-dependencies && default-workspace-template-install-dependencies

# ============================================================================
# End pre-COPY manifest layer. Source-changing layers begin below.
# ============================================================================

# copy in all of our code:
COPY . /mngr/code/

# Build the workspace from full source. Shared verbatim with the Lima provider.
RUN bash /mngr/code/scripts/build_workspace.sh

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
# The seed/relocate dance is docker-volume-specific; the Lima provider does
# not use it (the project syncs straight onto the VM's btrfs /mngr disk).
#
# Source mode bits are already +x; chmod is defensive in case the file
# is checked out without exec bits.
COPY scripts/default_workspace_template_seed.sh /usr/local/bin/default-workspace-template-seed
RUN chmod +x /usr/local/bin/default-workspace-template-seed
