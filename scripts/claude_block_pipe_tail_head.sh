#!/usr/bin/env bash
set -euo pipefail

# Block commands that pipe through tail or head.
# Instead, the agent should run the command redirected to a file and then read from it.

# Read JSON input from stdin
input=$(cat)

# Extract the command from tool_input.command using jq
command=$(echo "$input" | jq -r '.tool_input.command // empty')

# Nothing to check
if [[ -z "$command" ]]; then
    exit 0
fi

# Check if the command pipes through tail or head (e.g. "| tail -20", "| head -5")
# Match: pipe followed by optional whitespace, then tail or head, optionally with args
if echo "$command" | grep -qE '\|\s*(tail|head)(\s|$)'; then
    echo "Do not pipe commands through tail or head. Instead, redirect output to a temp file (e.g. cmd > /tmp/output.txt) and then read from that file separately using the Read tool or a separate tail/head command on the file." >&2
    exit 2
fi

exit 0
