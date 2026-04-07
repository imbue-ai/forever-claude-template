#!/usr/bin/env bash
# Wrapper script for ttyd that:
# 1. Runs ttyd with dynamic port allocation (-p 0)
# 2. Detects the assigned port from ttyd's stderr output
# 3. Registers it via forward_port.py
# 4. Forwards all output immediately for debugging
#
# Used as a service in services.toml.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

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
fi

PORT_REGISTERED=false

# Run ttyd and tee its stderr to detect the port
ttyd -p 0 -a -t disableLeaveAlert=true -W bash -c "$DISPATCH_SCRIPT" 2>&1 | while IFS= read -r line; do
    # Forward all output immediately
    echo "$line" >&2

    # Detect port assignment
    if [ "$PORT_REGISTERED" = "false" ] && echo "$line" | grep -q "Listening on port:"; then
        TTYD_PORT=$(echo "$line" | awk '{print $NF}')
        python3 "$REPO_ROOT/scripts/forward_port.py" --name terminal --url "http://localhost:$TTYD_PORT"
        PORT_REGISTERED=true

        # Also write the server event directly for backwards compatibility
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
    fi
done
