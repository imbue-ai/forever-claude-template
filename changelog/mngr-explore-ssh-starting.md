- Made workspaces recover automatically after a container/VM restart, even when
  the minds desktop app is not running:

  - The `docker`, `pool_host`, and `imbue_cloud` create templates now run their
    agent container with `--restart=unless-stopped`, so the container comes back
    after a docker daemon restart or host reboot. The container entrypoint (in
    the vendored mngr) then self-heals sshd, so the host is reachable again
    without a manual `mngr start`.

  - The `lima` template installs a systemd unit in the VM that runs
    `mngr start system-services` on boot (the agent runs directly in the VM in
    lima mode).

  - The `pool_host` bake installs a systemd unit on the outer pool/slice VM
    (via the new mngr `post_host_create_outer_command` hook) that, on VM boot,
    starts the agent container and relaunches the system-services agent inside
    it. The unit finds the container by mngr label, so it survives container
    rebuilds.

- Added `scripts/minds_lima_autostart.sh` and `scripts/minds_outer_autostart.sh`
  to back the two boot units.
