- Support for in-memory persistent dockview terminals (see the
  `system_interface` changelog for the user-facing feature). Root-level pieces:

- `scripts/run_ttyd.sh` now writes a `session.sh` ttyd dispatch that attaches
  to (or creates) a named tmux session per terminal tab and records the tab's
  pty for live title tracking.

- `scripts/terminal_tmux.conf` (new, sourced from `~/.tmux.conf` via
  `.mngr/settings.toml`) raises tmux `history-limit` to 10000, sets
  `window-size latest`, and installs the `client-session-changed` /
  `session-renamed` hooks that drive tab-title tracking.

- `scripts/notify_terminal_session.py` (new) is the best-effort helper those
  hooks call to notify the system_interface of session switches/renames.
