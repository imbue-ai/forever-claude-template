#!/usr/bin/env bash
# fit_terminal_window.sh -- keep a terminal-<N> session's tmux window sized to its client.
#
# Runs from the global client-attached / client-resized hooks in
# scripts/terminal_tmux.conf (via `run-shell -b`). tmux's `window-size latest`
# policy only re-evaluates a window's size on client INPUT activity, not on a
# bare pty resize -- and a dockview terminal tab resizes without input all the
# time (its iframe loads hidden at ~2x1 and grows when the tab is shown; sash
# drags resize without focus). Without this, the window sticks at its birth
# size (e.g. 2x1: one letter per line) until the user clicks into the terminal.
#
# The window size is read fresh from tmux at act time -- and re-checked once
# after a short settle -- rather than captured from the hook arguments: a
# resize burst fires many overlapping instances, and with captured geometry
# whichever resize-window landed last could pin the window at a stale
# intermediate size (resize-window implicitly sets window-size=manual, so
# nothing else would correct it). With act-time reads, whichever instance acts
# last converges the window on the real client size. Same scheme as mngr's
# sigwinch_panes.sh uses for agent windows; agent sessions are excluded here
# because those hooks already own them.

set -uo pipefail

SESSION="${1:?session name required}"

case "${SESSION}" in
    terminal-*) ;;
    *) exit 0 ;;
esac

# Seconds before the convergence re-check (overridable for tests).
FIT_SETTLE_SECONDS="${MINDS_TERMINAL_FIT_SETTLE_SECONDS:-1}"

# Resize the session's (single) window to its most-recently-active client.
_fit() {
    local size width height
    size="$(tmux list-clients -t "=${SESSION}" -F '#{client_activity} #{client_width} #{client_height}' 2>/dev/null \
        | sort -rn | awk 'NR==1 {print $2, $3}')"
    [ -n "${size}" ] || return 0
    width="${size% *}"
    height="${size#* }"
    tmux resize-window -t "=${SESSION}:" -x "${width}" -y "${height}" 2>/dev/null || true
}

_fit
sleep "${FIT_SETTLE_SECONDS}"
_fit
exit 0
