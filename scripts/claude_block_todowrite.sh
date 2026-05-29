#!/usr/bin/env bash
# PreToolUse hook for TodoWrite: deny with a message redirecting the agent
# to the project's task primitive (`tk`). The chat UI renders task progress
# from tk tickets, so dual-tracking with TodoWrite would split the source
# of truth. CLAUDE.md "Task management" describes the protocol.
set -euo pipefail

# Drain stdin (we ignore it; we always deny TodoWrite calls).
cat > /dev/null

jq -n '{
  hookSpecificOutput: {
    hookEventName: "PreToolUse",
    permissionDecision: "deny",
    permissionDecisionReason: "TodoWrite is disabled in this project. Use `tk` (the vendored ticket tracker) for task tracking. The chat UI renders progress from tk tickets, not from TodoWrite. See CLAUDE.md > Task management for the lifecycle (`tk create` -> `tk start` -> `tk add-note` -> `tk close`)."
  }
}'
