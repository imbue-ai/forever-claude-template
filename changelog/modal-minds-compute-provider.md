- Added a `[create_templates.modal]` block so the minds app can launch a
  workspace on a Modal sandbox (the "modal" compute provider). Like the lima
  template, Modal has no Dockerfile build, so the toolchain is provisioned over
  SSH after the sandbox boots by reusing the same `setup_system.sh` /
  `install_dependencies.sh` / `build_workspace.sh` scripts a Dockerfile-built
  workspace runs. It sets `provider = "modal"`, forwards the Anthropic creds +
  `GH_TOKEN`, and sets `idle_mode = "disabled"`. There is intentionally no
  autostart unit (the lima-autostart step is omitted): Modal sandboxes are
  ephemeral (~1 day) and do not survive a reboot, so the minds desktop app
  re-creates them rather than relying on systemd.

- The minds-facing Modal sizing + timeouts live in this template's
  `setting__extend` (rather than in the `mngr_modal` provider defaults): it
  enables the default-disabled provider (`providers.modal.is_enabled=true`) and
  sets `default_cpu=2.0` / `default_memory=4.0` (matching the lima/docker
  convention) plus a 24h `default_sandbox_timeout=86310` / `default_idle_timeout=86400`.
  The sandbox timeout plus the provider's 90s shutdown buffer lands exactly at
  Modal's 86400s (24h) hard cap. Paired with the provider-side PR
  imbue-ai/mngr#2310, which reverts those values to the provider's minimal
  defaults so this template owns them.
