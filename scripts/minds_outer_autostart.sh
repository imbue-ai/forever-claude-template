#!/bin/sh
# ExecStart body for the outer-VM minds autostart systemd unit (imbue_cloud /
# pool-host mode).
#
# In imbue_cloud mode the agent runs in a docker container inside the pool/slice
# VM, and mngr is installed inside that container. On a VM reboot the container
# is restored by its docker `--restart` policy and sshd self-heals via the
# container entrypoint, but the agent tmux session + supervisord stack are only
# re-established by `mngr start`. This script (run on the outer VM by the unit
# that scripts/install runs on boot) starts every mngr-managed agent container
# and relaunches the "system-services" agent inside it.
#
# Containers are found by the fixed mngr label namespace rather than a baked-in
# name, so the unit keeps working across container rebuilds. `mngr start` is
# idempotent and flock-serialized, so a redundant run (e.g. racing the desktop
# client, or a container that the restart policy already started) is harmless.
#
# `bash -lc` is a login shell, so it picks up /root/.local/bin via the FCT image
# PATH but NOT /mngr/env; source /mngr/env explicitly so mngr resolves the agent
# with the same host_dir/prefix context it was created with.
set -u

for container_id in $(docker ps -aq --filter "label=com.imbue.mngr.host-id"); do
    docker start "$container_id" >/dev/null 2>&1 || true
    docker exec --workdir / "$container_id" \
        bash -lc 'set -a; [ -f /mngr/env ] && . /mngr/env; set +a; mngr start system-services' || true
done
