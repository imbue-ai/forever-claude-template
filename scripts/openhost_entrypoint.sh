#!/usr/bin/env bash
# OpenHost entrypoint for the minds app.
#
# Replaces the desktop client + outer mngr provisioning: puts all durable
# state on the app-data volume, seeds the workspace on first boot, wires the
# LLM gateway and latchkey service URLs into the host env, creates the
# system-services agent (whose bootstrap window execs supervisord, which runs
# system_interface and friends), and then idles as PID 1 tailing supervisor
# logs to container stdout.
set -euo pipefail

: "${OPENHOST_APP_DATA_DIR:?OPENHOST_APP_DATA_DIR must be set}"
: "${OPENHOST_ROUTER_URL:?OPENHOST_ROUTER_URL must be set}"
: "${OPENHOST_APP_TOKEN:?OPENHOST_APP_TOKEN must be set}"

# Answer HTTP on the app port right away so the router's readiness window
# can't expire during a slow first boot (workspace seed + agent create can
# exceed it); system_interface's supervisord program stops it via
# scripts/openhost_stop_placeholder.sh before binding the port.
SYSTEM_INTERFACE_HOST=0.0.0.0 python3 /usr/local/bin/openhost-boot-placeholder &

# All durable state (mngr host dir, workspace, worktrees) lives under the
# persistent app-data dir; /mngr is the hard-coded path the template uses.
mngr_root="$OPENHOST_APP_DATA_DIR/mngr"
mkdir -p "$mngr_root"
if [ -d /mngr ] && [ ! -L /mngr ]; then
    # Image cruft only; rmdir refuses non-empty dirs so real data can't be lost.
    rmdir /mngr/code 2>/dev/null || true
    rmdir /mngr
fi
ln -sfn "$mngr_root" /mngr

# Seed /mngr/code from the image on first boot (no-op on warm boots; never
# overwrites agent edits).
default-workspace-template-seed

# The workspace is a git repo the agents commit into; the image copy ships
# without .git (excluded via .dockerignore), so initialize on first boot.
if [ ! -e /mngr/code/.git ]; then
    git -C /mngr/code init -b main -q
fi

# Host env file: mngr auto-sources it (set -a) into every agent shell, and
# bootstrap/supervisord inherit it. mngr rewrites this file as plain KEY=VALUE
# lines, so upsert per key rather than using a marker block.
env_file=/mngr/env
touch "$env_file"
set_host_env() {
    local key="$1" value="$2" tmp_env
    tmp_env="$(mktemp)"
    grep -v "^${key}=" "$env_file" > "$tmp_env" || true
    printf '%s=%s\n' "$key" "$value" >> "$tmp_env"
    mv "$tmp_env" "$env_file"
}

set_host_env MNGR_HOST_DIR /mngr
set_host_env OPENHOST_APP_NAME "${OPENHOST_APP_NAME:-minds}"
set_host_env OPENHOST_ROUTER_URL "$OPENHOST_ROUTER_URL"
set_host_env OPENHOST_APP_TOKEN "$OPENHOST_APP_TOKEN"
set_host_env OPENHOST_ZONE_DOMAIN "${OPENHOST_ZONE_DOMAIN:-}"
set_host_env ANTHROPIC_BASE_URL "$OPENHOST_ROUTER_URL/api/services/v2/call/llm/anthropic"
set_host_env ANTHROPIC_AUTH_TOKEN "$OPENHOST_APP_TOKEN"
set_host_env LATCHKEY_GATEWAY "$OPENHOST_ROUTER_URL/api/services/v2/call/latchkey"
set_host_env SYSTEM_INTERFACE_HOST 0.0.0.0

# The create templates' [commands.create].host_env__extend (see
# template/.mngr/settings.toml) only applies when mngr creates a NEW host; the
# in-container create targets the local provider's existing default host, so
# those vars must be written here. Keep in sync with settings.toml.
set_host_env IS_SANDBOX 1
set_host_env IS_AUTONOMOUS 1
set_host_env CLAUDE_CODE_ENABLE_OPUS_4_7_FAST_MODE 1
set_host_env CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK 1
set_host_env DISABLE_AUTOUPDATER 1
set_host_env CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC 1
set_host_env CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY 1
set_host_env ENABLE_CLAUDEAI_MCP_SERVERS false
set_host_env TICKETS_DIR /mngr/code/runtime/tickets
set_host_env OOM_PRIORITY_RUNTIME_DIR /mngr/code/runtime/oom_priority
set_host_env OPENSSL_armcap 0

export MNGR_HOST_DIR=/mngr


# mngr reads .mngr/settings.toml from the cwd.
cd /mngr/code

first_boot_marker=/mngr/openhost_first_boot_done
if [ ! -e "$first_boot_marker" ]; then
    mngr create system-services --template main --transfer none --label is_primary=true --no-connect --format json
    touch "$first_boot_marker"
else
    sh /mngr/code/scripts/minds_start_services_agent.sh &
fi

# PID 1 idles here; surface supervisord's log (written once bootstrap execs
# supervisord inside the services agent) on container stdout for `oh app logs`.
exec tail -n +1 -F /var/log/supervisor/supervisord.log
