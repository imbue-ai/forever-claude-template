- Added a locked Nix flake for the Docker/NixOS workspace system package set, so the Nix-managed tools resolve through a committed `flake.lock` instead of the base image's ambient `<nixpkgs>` channel.

- Updated the Docker/NixOS Dockerfile to build the workspace system environment from the locked flake while leaving Docker base-image digest pinning as a later golden-image hardening step.

- Moved the Docker/NixOS workspace package set to Node 24 because the locked `nixpkgs` revision marks Node 20 as insecure/end-of-life, and the workspace still needs npm for global CLI installation.

- Updated the Docker image contract smoke test so the Debian Dockerfile continues to expect Node 20 while the Docker/NixOS path expects Node 24 by default.

- Switched the Docker/NixOS workspace flake input from `nixos-unstable` to the current stable `nixos-26.05` branch.

- Pinned the Docker/NixOS workspace base image by digest and added a checked-in Nix closure manifest for the verified `aarch64-linux` build, so the image build fails if the resolved Nix system package closure changes unexpectedly. Added an explicit manifest regeneration script for intentional closure updates.

- Added an `/etc/fonts/fonts.conf` compatibility path in the Docker/NixOS image so Playwright's Chromium can load fontconfig and render text-heavy pages reliably.

- Refactored the Docker/NixOS Dockerfile to delegate Nix profile setup, closure verification, and compatibility shims to a parallel `setup_system_nixos.sh` script so the Dockerfile structure stays close to the Debian Dockerfile.

- Moved the Docker/NixOS Dockerfile to `nix/Dockerfile` so IDEs recognize it as a Dockerfile while keeping the repo-root Docker build context.

- Exposed the Docker provider bootstrap commands from the Nix profile on the image's default root `PATH`, preventing the provider from falling back to Debian `apt-get` package installation before SSH profile setup runs.
