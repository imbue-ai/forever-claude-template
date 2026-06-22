---
name: edit-services
description: Add, modify, or remove background services managed by supervisord. Use this when you want to run a long-lived (or one-shot) process alongside your main agent.
---

# Managing services

Background services are defined as `[program:<name>]` sections in
`supervisord.conf` at the repo root. `uv run bootstrap` runs first-boot setup
and then `exec`s `supervisord` in the foreground (in the `bootstrap` tmux
window); supervisord starts and supervises every program. Unlike the old
service manager, supervisord does **not** watch the config file -- you apply
changes with `supervisorctl`.

## Program format

```ini
[program:my-service]
command=uv run my-service
directory=/mngr/code
autostart=true
autorestart=true
startretries=1000000
stopasgroup=true
killasgroup=true
stdout_logfile=/var/log/supervisor/my-service-stdout.log
stderr_logfile=/var/log/supervisor/my-service-stderr.log
stdout_logfile_maxbytes=10MB
stderr_logfile_maxbytes=10MB
stdout_logfile_backups=3
stderr_logfile_backups=3
```

Key fields:

- `command` -- the program to run. **supervisord exec's this directly (no
  shell)**, so anything that chains with `&&`, sets an inline env var, or uses
  other shell syntax must be wrapped in `bash -c "..."`:

  ```ini
  command=bash -c "python3 scripts/forward_port.py --url http://localhost:8090 --name foo && uv run foo"
  ```
- `directory=/mngr/code` -- run from the repo root, so cwd-relative paths
  (`runtime/...`, `scripts/...`) resolve. Set this on every program.
- `autostart=true` -- start when supervisord boots.
- `autorestart=true` -- restart a long-lived daemon whenever it exits. (This is
  the replacement for the old `restart = "on-failure"`; the standard daemons all
  use `true`.) For a **one-shot** task that should run once and then stay
  stopped, use `autorestart=false` plus `startsecs=0` and `exitcodes=0` (see the
  `deferred-install` program for an example).
- `stopasgroup=true` / `killasgroup=true` -- signal the whole `bash -c` process
  group on stop, so a wrapped command shuts down cleanly.
- `stdout_logfile` / `stderr_logfile` (+ `*_maxbytes` / `*_backups`) --
  separate, rotated, container-local logs under `/var/log/supervisor/`. These
  are **not** under `runtime/`, so they are not backed up. If you omit them,
  supervisord writes AUTO logs into its `childlogdir` (`/var/log/supervisor`)
  instead.

Services inherit the agent environment (`MNGR_AGENT_STATE_DIR`,
`CLAUDE_CONFIG_DIR`, `MNGR_HOST_DIR`, `GH_TOKEN`, ...) from the bootstrap shell
that launched supervisord -- you do not need a per-program `environment=`.

## Adding a service

1. Add a new `[program:<name>]` section to `supervisord.conf`.
2. Apply it:

   ```bash
   supervisorctl reread && supervisorctl update
   ```

   `reread` re-parses the config; `update` starts newly-added programs, stops
   removed ones, and restarts changed ones.
3. Confirm it is running: `supervisorctl status <name>`.

## Removing a service

1. Delete the `[program:<name>]` section from `supervisord.conf`.
2. `supervisorctl reread && supervisorctl update` -- supervisord stops and
   forgets the removed program.

## Modifying a service

1. Change the program's `command` (or other fields) in `supervisord.conf`.
2. `supervisorctl reread && supervisorctl update` applies the change (it
   restarts the program when its definition changed). To bounce a program
   without editing its config, use `supervisorctl restart <name>`.

## Inspecting services

```bash
supervisorctl status                 # all programs + states
supervisorctl status <name>          # one program
supervisorctl tail -f <name> stderr  # follow a program's stderr log
```

Or read the log files directly under `/var/log/supervisor/`.

## Important

- Program names must be valid supervisord program names (no spaces).
- supervisord only manages the programs in `supervisord.conf`; it does not touch
  the main agent window or other tmux windows.
- If you need a one-off command, just run it directly rather than adding a
  program.
- For standing up a new web service (Flask lib or wrapping a third-party
  server), use the `build-web-service` skill -- it generates the `[program:*]`
  block and `forward_port.py` wiring for you.
