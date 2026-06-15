- Added a `[create_templates.aws]` block so the minds app can launch a
  workspace on an AWS EC2 instance (the new "AWS" compute provider). Like the
  vultr/ovh templates it runs the agent in a runsc-hardened Docker container on
  the EC2 outer host, forwards the Anthropic creds + `GH_TOKEN`, and seeds
  `/mngr/code` on first boot. It declares no `provider` (minds addresses the
  create as `system-services@<host>.aws-<region>`, selecting the region-specific
  provider block minds writes at startup) and sets `idle_mode = "disabled"` with
  no `auto_shutdown_seconds`, so the host stays up for the whole session.
