# bootstrap

First-boot setup for a default-workspace-template host, followed by launching
[supervisord](http://supervisord.org/), which supervises every background
service.

## CLI

- `bootstrap` - Run first-boot setup, then `exec` supervisord in the foreground.
  Invoked once per container boot from the `bootstrap` extra_window (see
  `.mngr/settings.toml`).

## What it does

`uv run bootstrap` runs, in order:

1. **Global git config** - rewrites `git@`/`ssh://` GitHub remotes to `https://`.
   (`core.hooksPath` is deliberately NOT set here: the post-commit auto-push
   hook only becomes active when the opt-in github-sync skill wires it up --
   see `libs/github_sync/README.md`.)
2. **CLAUDE_CONFIG_DIR host-env write** - records the services agent's per-agent
   Claude config dir in `$MNGR_HOST_DIR/env` so every other agent on the host
   inherits it.
3. **Initial chat agent** - on first boot only (gated by
   `runtime/initial_chat_created`), commits the rsynced workspace onto a clean
   `main` branch and creates the welcome chat agent (`--message /welcome`).
4. **Launch supervisord** - `exec supervisord -n -c supervisord.conf`. Running
   via `exec` keeps the bootstrap tmux window alive as supervisord and lets the
   supervised services inherit this shell's already-sourced agent environment.

## Services (supervisord)

Services are defined as `[program:*]` sections in `supervisord.conf` at the repo
root, not managed by this package. supervisord starts them, restarts the
long-lived ones when they exit (`autorestart=true`), and runs one-shot programs
(like `deferred-install`) exactly once per boot (`autorestart=false`).

Services inherit the agent environment from the bootstrap shell that exec'd
supervisord (there is no per-service `environment=` enumeration). Each program
writes separate, rotated, container-local logs under
`/var/log/supervisor/<name>-stdout.log` and `<name>-stderr.log` (not under
`runtime/`, so they are not backed up).

To add, change, or remove a service, edit `supervisord.conf` and run
`supervisorctl reread && supervisorctl update` (and `supervisorctl restart
<name>` to bounce one). See the `edit-services` skill for details.

## Deferred-install service

The `deferred-install` program in `supervisord.conf` runs
`scripts/deferred_install.sh`, which installs packages that are too heavy to
bake into the Docker image but aren't required by any boot-time service.
Currently it covers Playwright's Chromium browser + its apt system libraries
(`uv run playwright install --with-deps chromium`).

It is a one-shot supervisord program (`autorestart=false`, `startsecs=0`,
`exitcodes=0`): supervisord starts it once on boot and leaves it stopped after a
clean exit. The script is also **idempotent per image**: each deferred package
gets its own marker file at `/var/lib/minds/deferred-install/done.<package>`,
and the script skips any package whose marker already exists. The marker lives
at a container-local path (not in `runtime/`), so:

- A container restart on the same image sees the marker, skips re-install, and
  exits immediately. Package versions never silently change on restart -- the
  agent decides when to upgrade.
- A fresh image build wipes the marker, so the install runs exactly once on the
  new image's first boot.

To add another deferred package, add an `_install_<name>` function plus a
matching call in `main()` in `scripts/deferred_install.sh`. Keep installs
independent: a failure in one must not skip the others, and each must write its
own per-package marker only on success.

If something tries to use a deferred package before its install has finished, it
will fail loudly -- that is acceptable. Check
`/var/lib/minds/deferred-install/done.<package>`, or
`supervisorctl status deferred-install` and
`/var/log/supervisor/deferred-install-stdout.log`, before using browser
automation in a fresh workspace.
