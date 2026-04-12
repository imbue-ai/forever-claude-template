#!/usr/bin/env bash
# Wrapper script for ttyd that:
# 1. Runs ttyd on a fixed, known-by-convention port (7681)
# 2. Registers the port via forward_port.py before starting ttyd
# 3. Writes server events for discovery
#
# Started as an extra_window (not via bootstrap/services.toml) so that
# terminal access is always available even if bootstrap fails.
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
    if [ ! -f "$MNGR_AGENT_STATE_DIR/commands/ttyd/agent.sh" ]; then
        cat > "$MNGR_AGENT_STATE_DIR/commands/ttyd/agent.sh" << 'AGENT_SCRIPT'
#!/bin/bash
set -euo pipefail
_SESSION=$(tmux display-message -p '#{session_name}')
unset TMUX
exec tmux attach -t "$_SESSION":0
AGENT_SCRIPT
        chmod +x "$MNGR_AGENT_STATE_DIR/commands/ttyd/agent.sh"
    fi
    if [ ! -f "$MNGR_AGENT_STATE_DIR/commands/ttyd/workdir.sh" ]; then
        cat > "$MNGR_AGENT_STATE_DIR/commands/ttyd/workdir.sh" << 'WORKDIR_SCRIPT'
#!/bin/bash
cd "$1" 2>/dev/null && exec bash
WORKDIR_SCRIPT
        chmod +x "$MNGR_AGENT_STATE_DIR/commands/ttyd/workdir.sh"
    fi
fi

# Register the terminal port before starting ttyd (port is known ahead of time)
uv run python3 "$REPO_ROOT/scripts/forward_port.py" --name terminal --url "http://localhost:$TTYD_PORT"

# Write server events for discovery
if [ -n "${MNGR_AGENT_STATE_DIR:-}" ]; then
    mkdir -p "$MNGR_AGENT_STATE_DIR/events/servers"
    _TS=$(date -u +"%Y-%m-%dT%H:%M:%S.000000000Z")
    _EID="evt-$(echo -n "terminal:http://localhost:$TTYD_PORT" | sha256sum | cut -c1-32)"
    printf '{"timestamp":"%s","type":"server_registered","event_id":"%s","source":"servers","server":"terminal","url":"http://localhost:%s"}\n' \
        "$_TS" "$_EID" "$TTYD_PORT" \
        >> "$MNGR_AGENT_STATE_DIR/events/servers/events.jsonl"

    # Also register the agent sub-URL
    if [ -f "$MNGR_AGENT_STATE_DIR/commands/ttyd/agent.sh" ]; then
        _EID2="evt-$(echo -n "agent:http://localhost:$TTYD_PORT?arg=agent" | sha256sum | cut -c1-32)"
        printf '{"timestamp":"%s","type":"server_registered","event_id":"%s","source":"servers","server":"agent","url":"http://localhost:%s?arg=agent"}\n' \
            "$_TS" "$_EID2" "$TTYD_PORT" \
            >> "$MNGR_AGENT_STATE_DIR/events/servers/events.jsonl"
    fi
fi

# Start ttyd on the fixed port (exec replaces this shell for clean process management)
exec ttyd -p "$TTYD_PORT" -a -t disableLeaveAlert=true -W bash -c "$DISPATCH_SCRIPT"
