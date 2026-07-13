#!/usr/bin/env bash
#
# with_agent_env.sh -- run a command with the agent environment restored.
#
# cron constructs a minimal environment for its jobs (roughly just
# HOME/PATH/SHELL/LOGNAME), so none of the agent env survives into them:
# MNGR_HOST_DIR, MNGR_AGENT_ID, LATCHKEY_*, GH_TOKEN, the PATH that puts uv at
# /root/.local/bin, and so on. Bootstrap snapshots its full environment to
# /run/minds-agent-env on every boot; this wrapper sources that snapshot, cds to
# the repo root, and execs the given command. Every cron job -- the built-in
# Caretaker entry and any user-added job alike -- should be prefixed with it:
#
#   17 3 * * *   root   /mngr/code/scripts/with_agent_env.sh bash scripts/my_job.sh >> /var/log/supervisor/my-job.log 2>&1
set -euo pipefail

ENV_SNAPSHOT=/run/minds-agent-env
if [ ! -f "$ENV_SNAPSHOT" ]; then
    echo "with_agent_env.sh: $ENV_SNAPSHOT not found -- bootstrap writes it at boot, so either the container is still starting or bootstrap failed" >&2
    exit 1
fi
set -a
# shellcheck disable=SC1090
. "$ENV_SNAPSHOT"
set +a
cd /mngr/code
exec "$@"
