#!/usr/bin/env bash
# PreToolUse hook for TaskCreate/TaskList/TaskUpdate: deny with a message
# redirecting the agent to `tk` for task management. Claude Code's built-in
# teams/task system conflicts with the tk-based progress tracking that the
# chat UI renders. CLAUDE.md "Task management" describes the protocol.
set -euo pipefail

# Drain stdin (we ignore it; we always deny Task* calls).
cat > /dev/null

jq -n '{
  hookSpecificOutput: {
    hookEventName: "PreToolUse",
    permissionDecision: "deny",
    permissionDecisionReason: "Claude Code built-in Task tools (TaskCreate, TaskList, TaskUpdate) are disabled in this project. Use `tk` for task management -- `tk create --step` for per-turn progress tracking, `tk create` for cross-agent tickets. The chat UI renders progress from tk records, not Claude Code tasks. See CLAUDE.md > Task management."
  }
}'
