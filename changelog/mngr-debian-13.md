- Pinned the agent container's base image to a specific Debian release. The
  Dockerfile now uses `FROM python:3.12.13-slim-bookworm` instead of the
  codename-less `python:3.12.13-slim`. The unsuffixed tag floats to whatever
  Debian release is current upstream (it had already drifted to Debian 13
  "trixie"), which silently diverged the container OS from the rest of the
  mngr fleet (Lima VMs, OVH/AWS/Vultr VPS images, and the `debian:bookworm-slim`
  container default are all Debian 12 "bookworm"). Pinning to `-bookworm` puts
  the agent container back on Debian 12 to match every other environment and
  makes the OS deterministic so it no longer drifts on upstream rebuilds. The
  Python patch version (`3.12.13`) is unchanged.
