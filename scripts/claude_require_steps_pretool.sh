#!/usr/bin/env bash
# PreToolUse hook: soft-block substantive tool calls when the agent has no
# in_progress step record. Injects a system reminder the agent can see while
# letting the tool call proceed, rather than hard-blocking (exit 2).
#
# IMPORTANT: for PreToolUse, plain stdout on exit 0 is written only to the
# debug log -- the agent never sees it (unlike UserPromptSubmit/SessionStart,
# where stdout is added to context). To reach the agent without blocking, the
# reminder must be emitted as JSON via hookSpecificOutput.additionalContext,
# which Claude Code injects as a system reminder next to the tool result.
# See https://code.claude.com/docs/en/hooks.
#
# Skipped for read-only / introspection tools (Read, Glob, Grep, etc.) and
# for Bash commands that invoke tk itself (so the agent can create steps).
set -euo pipefail

# Emit a non-blocking PreToolUse reminder as additionalContext, then exit.
emit_reminder() {
    jq -n --arg ctx "$1" \
        '{hookSpecificOutput: {hookEventName: "PreToolUse", additionalContext: $ctx}}'
    exit 0
}

repo_root="${MNGR_AGENT_WORK_DIR:-$(pwd)}"
tickets_dir="${TICKETS_DIR:-${repo_root}/.tickets}"

input=$(cat)

# Skip if subagent proxy child -- subagents manage their own steps.
[[ -z "${MNGR_CLAUDE_SUBAGENT_PROXY_CHILD:-}" ]] || exit 0

tool_name=$(echo "$input" | jq -r '.tool_name // empty')

# Tools that don't count as "substantive work" -- the agent should be free
# to read, search, and navigate without declaring steps first.
case "$tool_name" in
    Read|Glob|Grep|WebFetch|WebSearch|ToolSearch|Skill|\
    TaskCreate|TaskUpdate|TaskGet|TaskList|TaskOutput|TaskStop|\
    LSP|Monitor|SendMessage|EnterPlanMode|ExitPlanMode|\
    mcp__sculptor__ask_user_question|mcp__sculptor__exit_plan_mode)
        exit 0
        ;;
esac

# For Bash calls, skip if the command is invoking tk (creating/managing
# steps).
if [[ "$tool_name" == "Bash" ]]; then
    command=$(echo "$input" | jq -r '.tool_input.command // empty')
    case "$command" in
        tk\ *|*/tk\ *|*/ticket\ *|*tk\ *)
            exit 0
            ;;
    esac
fi

# If there's no tickets directory yet, skip -- the agent hasn't started
# using tk at all (possibly a brand-new session).
[[ -d "$tickets_dir" ]] || exit 0

tk_script="${repo_root}/vendor/tk/ticket"
[[ -x "$tk_script" ]] || exit 0

export TICKETS_DIR="$tickets_dir"

# Check for any in_progress step record owned by this agent.
in_progress=$("$tk_script" steps --status=in_progress 2>/dev/null | sed '/^[[:space:]]*$/d' || true)

if [[ -n "$in_progress" ]]; then
    exit 0
fi

# Also check for any open (not-yet-started) steps -- the agent declared
# a plan but hasn't called `tk start` yet on the first one.
open_steps=$("$tk_script" steps 2>/dev/null | sed '/^[[:space:]]*$/d' || true)

if [[ -n "$open_steps" ]]; then
    emit_reminder "
[Step tracking reminder]

You have declared step records but none is currently in_progress. Call \`tk start <id>\` on your next step before doing more work. Steps must be serial -- only one in_progress at a time.
"
fi

# No steps at all -- the agent is doing substantive work without declaring
# any plan.
emit_reminder "
[Step tracking reminder]

You are about to do work without declaring any step records. The chat progress view requires steps to render your work as a structured timeline.

Before continuing, declare your plan as step records (each prints \`Created <id>: <title>\`):
  tk create --step \"Description of first step\"
  tk create --step \"Description of second step\"
  ...
Then start the first step with its literal id: tk start <id>

See CLAUDE.md > Task management for the full protocol.
"
