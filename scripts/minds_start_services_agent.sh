#!/bin/sh
# Start the minds "system-services" agent, sourcing the same env mngr itself
# applies to agent operations: the host env first, then the agent's own env
# (agent overrides host) -- mirroring mngr's build_source_env_prefix
# (host_dir/env + host_dir/agents/<id>/env). Sourcing only the host env would
# violate that contract.
#
# Invoked by the minds boot units (the lima in-VM unit and the outer-VM unit's
# `docker exec`) so a workspace recovers after a VM/container restart without the
# desktop app. Run it through a login shell (`bash -lc`) so uv/mngr are on PATH.
#
# The agent's env file is keyed by agent id on disk, so we resolve the
# system-services agent by its name in data.json. `mngr start` is idempotent and
# flock-serialized, so racing the desktop client is safe.
set -eu

# Source host env first (sets MNGR_HOST_DIR etc.), then the system-services
# agent's env on top, with auto-export so `mngr` and the relaunched agent inherit them.
set -a
# shellcheck source=/dev/null
[ -f /mngr/env ] && . /mngr/env
host_dir="${MNGR_HOST_DIR:-/mngr}"
for data_file in "$host_dir"/agents/*/data.json; do
    [ -e "$data_file" ] || continue
    if [ "$(jq -r '.name // empty' "$data_file" 2>/dev/null)" = "system-services" ]; then
        agent_env="$(dirname "$data_file")/env"
        # shellcheck source=/dev/null
        [ -f "$agent_env" ] && . "$agent_env"
    fi
done
set +a

exec mngr start system-services
