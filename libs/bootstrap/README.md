# bootstrap

Service manager for the forever-claude template.

Reads `services.toml`, reconciles tmux windows to match, and watches for changes.

## CLI

- `bootstrap` - Start the service manager (runs in the foreground)

## Restart policy

Each service may declare a `restart` value in `services.toml`:

- `never` (default) - if the service exits, it stays stopped.
- `on-failure` - the service is restarted when it exits with a non-zero
  status.

A service runs inside a tmux window's shell, so the window stays open at an
idle shell after the service process exits -- its existence is not a
liveness signal. To detect exits, the keystrokes sent to the window append a
recorder that writes the command's exit status into the `@svc_exit_status`
window option once it returns. Every poll, the manager reads that option for
each managed window and restarts the service if its `restart` policy and
exit status call for it. A crash-looping service is therefore retried at
most once per poll interval.

## Deferred-install service

The `deferred-install` entry in `services.toml` runs `scripts/deferred_install.sh`,
which installs packages that are too heavy to bake into the Docker image but
aren't required by any boot-time service. Currently it covers Playwright's
Chromium browser + its apt system libraries (`uv run playwright install --with-deps chromium`).

The script is **idempotent and one-shot per image**: each deferred package
gets its own marker file at `/var/lib/minds/deferred-install/done.<package>`,
and the script skips any package whose marker already exists. The marker
lives at a container-local path (not in `runtime/`), so:

- A container restart on the same image sees the marker, skips re-install, and
  exits immediately. Package versions never silently change on restart -- the
  agent decides when to upgrade.
- A fresh image build wipes the marker, so the install runs exactly once on
  the new image's first boot.

To add another deferred package, add an `_install_<name>` function plus a
matching call in `main()` in `scripts/deferred_install.sh`. Keep installs
independent: a failure in one must not skip the others, and each must write
its own per-package marker only on success.

If something tries to use a deferred package before its install has finished,
it will fail loudly -- that is acceptable. The top-level `CLAUDE.md` documents
how to check the marker / tmux window before using browser automation in a
fresh workspace.
