---
name: edit-services
description: Add, modify, or remove background services managed by the bootstrap service manager. Use this when you want to run a long-lived process alongside your main agent.
---

# Managing services

Background services are defined in `services.toml` at the repo root. The bootstrap service manager watches this file and reconciles tmux windows to match.

## Format

```toml
[services.my-service]
command = "python3 my_script.py"
restart = "on-failure"  # optional: "on-failure" or "never" (default: "never")
```

Each `[services.<name>]` entry defines a service that will run in its own tmux window named `svc-<name>`.

## Adding a service

1. Add a new `[services.<name>]` section to `services.toml`
2. The bootstrap manager will detect the change and create a new tmux window running your command

## Removing a service

1. Delete the `[services.<name>]` section from `services.toml`
2. The bootstrap manager will detect the change and kill the corresponding tmux window

## Modifying a service

1. Change the `command` in the `[services.<name>]` section
2. The bootstrap manager will kill the old window and start a new one with the updated command

## Restart policy

- `never` (default): the service runs once. If it exits, it stays stopped.
- `on-failure`: the service is restarted if it exits with a non-zero exit code.

## Important

- Service names must be valid tmux window names (no spaces or special characters).
- The bootstrap manager only manages windows it created (prefixed with `svc-`). It does not touch the main agent window or other plugin-injected windows.
- If you need a one-off script, just run it directly rather than adding it as a service.
