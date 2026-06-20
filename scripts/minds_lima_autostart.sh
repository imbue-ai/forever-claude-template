#!/bin/sh
# Install + enable a systemd unit that relaunches the minds "system-services"
# agent whenever the Lima VM boots.
#
# In lima mode the agent runs directly in the VM as root (no nested container),
# and mngr is installed in the VM at /root/.local/bin (by build_workspace.sh).
# sshd is brought back by the VM's own systemd on boot, but mngr's agent tmux
# session + supervisord stack are not -- they are only re-established by
# `mngr start`. This unit runs that on boot so the workspace recovers from a VM
# reboot even when the minds desktop app is not running.
#
# Run once as root during lima provisioning (via the `extra_provision_command`
# create-template hook). Idempotent: re-running overwrites the unit and re-enables.
#
# `mngr start` itself is idempotent and serializes against any concurrent start
# (e.g. the desktop client) via a host-level flock, so this never double-launches.
set -eu

UNIT_PATH=/etc/systemd/system/minds-autostart.service

# `bash -lc` is a login shell, so it picks up /root/.local/bin via
# /etc/profile.d/fct_path.sh but NOT /mngr/env (that is wired into .bashrc, for
# interactive shells only). Source /mngr/env explicitly so mngr resolves the
# agent with the same host_dir/prefix context it was created with.
cat > "$UNIT_PATH" <<'UNIT'
[Unit]
Description=Start the minds system-services agent on boot
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=HOME=/root
ExecStart=/bin/bash -lc 'set -a; [ -f /mngr/env ] && . /mngr/env; set +a; mngr start system-services'

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable minds-autostart.service
