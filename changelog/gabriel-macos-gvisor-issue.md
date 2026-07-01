- The local `docker` provider no longer hardcodes the gVisor (`runsc`) runtime.
  `[providers.docker]` now leaves `docker_runtime` unset, so it uses Docker's
  default runtime (runc), which is available everywhere -- notably macOS, where
  `runsc` is not. This removes the need for the
  `MNGR__PROVIDERS__DOCKER__DOCKER_RUNTIME=runc` workaround when creating a
  docker workspace on a host without gVisor.

- Added a `docker_runsc` create-template overlay that opts a docker create into
  the gVisor runtime: `--template docker --template docker_runsc` reuses the
  entire `docker` template body and only switches the container runtime to
  `runsc`. The container-runtime choice is now the single difference between the
  default (runc) and hardened (runsc) paths, with no duplicated template body to
  keep in sync. The minds desktop app selects the overlay by default on Linux
  (where `runsc` is installed) and omits it on macOS.
