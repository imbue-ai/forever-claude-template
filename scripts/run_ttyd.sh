#!/usr/bin/env bash
# Wrapper script for ttyd that:
# 1. Runs ttyd on a fixed, known-by-convention port (7681)
# 2. Registers the port via forward_port.py before starting ttyd
# 3. Writes server events for discovery
#
# Runs as the supervisord `terminal` program (started by supervisord, which
# bootstrap launches), so terminal access is supervised and restarted alongside
# the other services.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

TTYD_PORT=7681

# Build the ttyd dispatch command (matches the mngr_ttyd plugin's approach)
DISPATCH_SCRIPT='
KEY="${1:-}"
if [ -z "$KEY" ]; then
  exec bash
fi
SCRIPT="$MNGR_AGENT_STATE_DIR/commands/ttyd/$KEY.sh"
if [ -f "$SCRIPT" ]; then
  shift
  exec bash "$SCRIPT" "$@"
fi
echo "Unknown ttyd key: $KEY" >&2
read -r
exit 1
'

# Ensure ttyd commands directory exists and has an agent dispatch script
if [ -n "${MNGR_AGENT_STATE_DIR:-}" ]; then
    mkdir -p "$MNGR_AGENT_STATE_DIR/commands/ttyd"
    # Rewrite agent.sh on every run so old deployments pick up the name-arg
    # support -- otherwise stale single-session copies keep attaching to the
    # primary agent regardless of the ?arg=agent&arg=<name> we now pass.
    cat > "$MNGR_AGENT_STATE_DIR/commands/ttyd/agent.sh" << 'AGENT_SCRIPT'
#!/bin/bash
# Attach to a mngr agent's tmux session window 0.
#
# If a session name is provided as $1, use "$MNGR_PREFIX$1" as the target
# session (so the minds chat UI can deep-link to a specific sub-agent's
# terminal by passing the agent name). Otherwise fall back to the current
# tmux session -- useful when ttyd is invoked without args.
set -euo pipefail
if [ $# -gt 0 ] && [ -n "$1" ]; then
    TARGET_SESSION="${MNGR_PREFIX:-mngr-}$1"
else
    TARGET_SESSION=$(tmux display-message -p '#{session_name}')
fi
unset TMUX
exec tmux attach -t "$TARGET_SESSION":0
AGENT_SCRIPT
    chmod +x "$MNGR_AGENT_STATE_DIR/commands/ttyd/agent.sh"
    if [ ! -f "$MNGR_AGENT_STATE_DIR/commands/ttyd/workdir.sh" ]; then
        cat > "$MNGR_AGENT_STATE_DIR/commands/ttyd/workdir.sh" << 'WORKDIR_SCRIPT'
#!/bin/bash
cd "$1" 2>/dev/null && exec bash
WORKDIR_SCRIPT
        chmod +x "$MNGR_AGENT_STATE_DIR/commands/ttyd/workdir.sh"
    fi
fi

# Serve the OSC 52-capable ttyd web client so a mouse-drag copy inside tmux
# reaches the system clipboard. The tmux config (~/.tmux.conf, written by the
# template's extra_provision_command) emits an OSC 52 escape on copy, but the
# stock ttyd 1.7.7 client has no OSC 52 handler and silently drops it; the
# patched client vendored with the mngr_ttyd plugin honors it. The mngr_ttyd
# plugin is disabled here (the terminal is a supervised service, not an mngr
# window), so we replicate its client install: decompress that vendored client
# and serve it via `ttyd -I`, falling back to the stock client if the asset is
# missing (so ttyd still starts).
TTYD_INDEX_FLAGS=()
TTYD_CLIENT_GZ="$REPO_ROOT/vendor/mngr/libs/mngr_ttyd/imbue/mngr_ttyd/resources/ttyd_index.html.gz"
if [ -n "${MNGR_AGENT_STATE_DIR:-}" ] && [ -f "$TTYD_CLIENT_GZ" ]; then
    TTYD_INDEX_PATH="$MNGR_AGENT_STATE_DIR/commands/ttyd/index.html"
    if gzip -dc "$TTYD_CLIENT_GZ" > "$TTYD_INDEX_PATH"; then
        TTYD_INDEX_FLAGS=(-I "$TTYD_INDEX_PATH")
    else
        echo "warning: failed to decompress ttyd web client at $TTYD_CLIENT_GZ; using stock client" >&2
        rm -f "$TTYD_INDEX_PATH"
    fi
fi

# Register the terminal port before starting ttyd (port is known ahead of time)
uv run python3 "$REPO_ROOT/scripts/forward_port.py" --name terminal --url "http://localhost:$TTYD_PORT"

# Write server events for discovery. The "agent" sub-URL is intentionally not
# registered as its own application: the chat UI exposes it via an inline link
# instead of a top-level application tile.
if [ -n "${MNGR_AGENT_STATE_DIR:-}" ]; then
    mkdir -p "$MNGR_AGENT_STATE_DIR/events/servers"
    _TS=$(date -u +"%Y-%m-%dT%H:%M:%S.000000000Z")
    _EID="evt-$(echo -n "terminal:http://localhost:$TTYD_PORT" | sha256sum | cut -c1-32)"
    printf '{"timestamp":"%s","type":"server_registered","event_id":"%s","source":"servers","server":"terminal","url":"http://localhost:%s"}\n' \
        "$_TS" "$_EID" "$TTYD_PORT" \
        >> "$MNGR_AGENT_STATE_DIR/events/servers/events.jsonl"
fi

# Start ttyd on the fixed port (exec replaces this shell for clean process management)
exec ttyd -p "$TTYD_PORT" -a -t disableLeaveAlert=true "${TTYD_INDEX_FLAGS[@]}" -W bash -c "$DISPATCH_SCRIPT"
