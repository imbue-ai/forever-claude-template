Fix the web terminal so a mouse-drag copy inside tmux reaches the system clipboard.

The terminal service (`scripts/run_ttyd.sh`) now serves the OSC 52-capable ttyd web client vendored with the `mngr_ttyd` plugin (via `ttyd -I`). Previously it served the stock ttyd client, which has no OSC 52 handler, so tmux copies were silently dropped even though the tmux config emits OSC 52. Falls back to the stock client if the vendored asset is missing.
