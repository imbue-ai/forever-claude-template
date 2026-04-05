#!/usr/bin/env bash
# Stop hook: always prevent Claude from exiting.
# Exit code 2 tells Claude Code to block the stop.

echo "You are a persistent agent. Check PURPOSE.md to understand your current goal and purpose. Run scripts/wait.sh to wait for the next message rather than ending your conversational turn." >&2
exit 2
