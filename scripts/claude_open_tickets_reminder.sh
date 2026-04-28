#!/usr/bin/env bash
# UserPromptSubmit hook: if any tk tickets are still open or in_progress when
# a new user message arrives, inject a system reminder so the agent knows
# what's outstanding before deciding what to do. Silent if there are no open
# tickets or no .tickets/ directory yet.
set -euo pipefail

repo_root="${MNGR_AGENT_WORK_DIR:-$(pwd)}"
tickets_dir="${repo_root}/.tickets"

# Drain stdin.
cat > /dev/null

[[ -d "$tickets_dir" ]] || exit 0

tk_script="${repo_root}/vendor/tk/ticket"
[[ -x "$tk_script" ]] || exit 0

# Setting TICKETS_DIR explicitly avoids tk's parent-walk falling back to a
# random ancestor when this hook runs from outside the repo root.
export TICKETS_DIR="$tickets_dir"

# `tk ready` is a built-in (no plugin-on-PATH needed) that lists every
# open + in_progress ticket whose deps are resolved. We don't use deps in
# this project, so for our purposes it lists every unfinished ticket.
# Output format:  <id>  [Pn][<status>] - <title>
open_lines=$("$tk_script" ready 2>/dev/null | sed '/^[[:space:]]*$/d' || true)

[[ -n "$open_lines" ]] || exit 0

cat <<EOF

[Open task reminder from forever-claude-template]

You have task tickets that are not yet closed:

$open_lines

For each one, decide before continuing: keep working on it (call \`tk start <id>\` if it's not already in_progress), replace it with a fresh ticket, or close it now with an honest summary. Every started ticket must terminate as closed via \`tk close <id>\` (with \`tk add-note <id> "<summary>"\` first to record what you accomplished). There is no "failed" status -- if a goal couldn't be reached, say so in the summary, then close.

See CLAUDE.md > Task management for the full protocol.
EOF
