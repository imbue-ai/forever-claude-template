#!/bin/sh
# Install + enable a systemd path+service pair that relaunches the minds
# "system-services" agent whenever the Lima VM boots.
#
# In lima mode the agent runs directly in the VM as root (no nested container),
# and mngr is installed in the VM at /root/.local/bin (by build_workspace.sh).
# sshd is brought back by the VM's own systemd on boot, but mngr's agent tmux
# session + supervisord stack are not -- they are only re-established by
# `mngr start`. This relaunches it on boot so the workspace recovers from a VM
# reboot even when the minds desktop app is not running.
#
# Run once as root during lima provisioning (via the `extra_provision_command`
# create-template hook). Idempotent: re-running overwrites the units and re-enables.
#
# `mngr start` itself is idempotent and serializes against any concurrent start
# (e.g. the desktop client) via a host-level flock, so this never double-launches.
#
# Boot ordering is the hard part: lima keeps /mngr on a separate btrfs disk and
# only mounts it AND creates the /mngr symlink (-> /mnt/lima-mngr-<hash>-data) at
# the very end of its per-boot cloud-init provisioning, whose duration is wildly
# variable (seconds to many minutes). Ordering against fstab/cloud-final or
# polling with a fixed timeout both lose that race. Instead we use a systemd
# .path unit that watches for /mngr/code and triggers the start service the
# moment the workspace appears -- event-driven, no timeout, no race.
set -eu

SERVICE_PATH=/etc/systemd/system/minds-autostart.service
PATH_UNIT=/etc/systemd/system/minds-autostart.path
START_SCRIPT=/mngr/code/scripts/minds_start_services_agent.sh

# The service: start the agent in its full env via the shared start script. No
# readiness wait needed -- the .path unit only triggers this once the script
# exists (i.e. /mngr is mounted + symlinked). `bash -lc` gives uv/mngr on PATH.
cat > "$SERVICE_PATH" <<UNIT
[Unit]
Description=Start the minds system-services agent on boot
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=HOME=/root
ExecStart=/bin/bash -lc 'exec $START_SCRIPT'
UNIT

# The path watcher: fire the service when the workspace start script appears.
cat > "$PATH_UNIT" <<UNIT
[Unit]
Description=Watch for the minds workspace and start system-services

[Path]
PathExists=$START_SCRIPT
Unit=minds-autostart.service

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable minds-autostart.path
# Start the watcher now too, so it works without a reboot: if /mngr is already
# mounted (the common case at provision time) the service fires immediately.
systemctl start minds-autostart.path
