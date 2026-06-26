- Made copy-paste work in the browser terminal. The default `~/.tmux.conf`
  written on every minds host now enables OSC 52 clipboard support
  (`set-clipboard on`, `terminal-features ,*:clipboard`, and copy-mode
  `copy-pipe-and-cancel` bindings), so selecting text with the mouse inside a
  tmux session copies it to the system clipboard. `mouse on` is kept, so
  mouse-wheel scroll and in-app mouse continue to work. This relies on the
  mngr_ttyd plugin serving an OSC 52-capable web client (the stock ttyd 1.7.7
  client silently drops these escapes).
