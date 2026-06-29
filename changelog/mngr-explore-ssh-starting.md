- Made workspaces recover automatically after a container/VM restart, even when
  the minds desktop app is not running:

  - The `docker`, `vultr`, `aws`, `pool_host`, and `imbue_cloud` create templates
    now run their agent container with `--restart=unless-stopped`, so the
    container comes back after a docker daemon restart or host reboot. The
    container entrypoint (in the vendored mngr) then self-heals sshd, so the host
    is reachable again without a manual `mngr start`.

  - The `lima` template installs a systemd path+service pair in the VM that runs
    `mngr start system-services` the moment the workspace volume appears on boot
    (the agent runs directly in the VM in lima mode). A path unit is used because
    lima mounts /mngr (a separate btrfs disk) at a highly variable point late in
    boot, so event-driven triggering avoids racing the mount.

  - The `vultr`, `aws`, and `pool_host` modes install a systemd unit on the
    outer VPS/VM (via the new mngr `post_host_create_outer_command` hook) that,
    on boot, starts the agent container and relaunches the system-services agent
    inside it. The unit finds the container by mngr label, so it survives
    container rebuilds. imbue_cloud pool hosts inherit this unit from the
    pool_host bake.

- Both boot paths relaunch the agent via `scripts/minds_start_services_agent.sh`,
  which sources the host env AND the system-services agent's own env (matching
  mngr's host-then-agent env contract) before `mngr start`.

- Added `scripts/minds_lima_autostart.sh` (in-VM lima boot unit),
  `scripts/minds_install_outer_autostart.sh` (installs the outer-VM boot unit),
  and `scripts/minds_start_services_agent.sh` (the shared env-sourcing start
  action both units run).
