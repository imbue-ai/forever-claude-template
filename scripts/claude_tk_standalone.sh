#!/usr/bin/env bash
# PreToolUse hook: HARD-BLOCK a `tk`/`ticket` start or close that is not run as
# the ONLY command in the tool call.
#
# Why: the chat progress view reconstructs each step's structure and grouping
# from two things in the transcript -- the tk command's visible OUTPUT (the
# `Updated <id> -> <status>` line) and the command's POSITION. If a start/close
# is chained with other commands (a leading `cd`, `&&`, `;`, `|`, `&`, a
# newline) or its output is redirected (`>`, `>>`, `2>`, `&>`, `</dev/null`,
# ...), the transition gets suppressed or mis-positioned, so the step never
# groups the work done under it. That is exactly what produced the
# "Confirm the refresh button works for you" bug, where the agent ran
# `cd /mngr/code; tk start <id> >/dev/null 2>&1; sed ...`: the start output was
# swallowed, the step was never seen as open, and its work + close fell out of
# the timeline as loose blocks. Forbidding the chained/redirected form makes
# that class of bug structurally impossible at the source.
#
# tk uses the TICKETS_DIR env var (absolute) and works from any directory, so a
# `cd` to the repo root is never needed.
#
# Scope: ONLY `start` and `close`. `create` is exempt -- agents legitimately
# batch several `tk create --step ...` up front when declaring the plan, and a
# create carries no positional transition the view must group around.
#
# Blocks via exit 2 with a stderr message the agent sees (mirrors
# claude_prevent_commit_rewrite.sh). Skipped for subagents (they manage their
# own progress view). The command parsing lives in the sibling
# claude_tk_standalone_check.py -- it shell-tokenizes the command with `shlex`
# (so a close summary, or a string that merely mentions "tk close", stays
# inside one quoted token and cannot trip the checks), which bash regex cannot
# do reliably.
set -euo pipefail

input=$(cat)

[[ -z "${MNGR_CLAUDE_SUBAGENT_PROXY_CHILD:-}" ]] || exit 0

tool_name=$(echo "$input" | jq -r '.tool_name // empty')
[[ "$tool_name" == "Bash" ]] || exit 0

command=$(echo "$input" | jq -r '.tool_input.command // empty')
[[ -n "$command" ]] || exit 0

script_dir=$(cd "$(dirname "$0")" && pwd)
exec python3 "$script_dir/claude_tk_standalone_check.py" "$command"
