#!/usr/bin/env bash
# UserPromptSubmit hook: if THIS agent has any tk STEP records (turn-bound
# progress markers) still open or in_progress when a new user message
# arrives, inject a system reminder so the agent knows what's outstanding
# before deciding what to do. Silent if there are no open steps or no
# .tickets/ directory yet.
#
# Step records, not tickets: regular tickets persist cross-agent and the
# agent is expected to manage them through `tk ls / tk ready / tk show`.
# Steps are the per-turn progress records that drive the chat progress
# view, and they are the only thing the carryover reminder is about.
set -euo pipefail

repo_root="${MNGR_AGENT_WORK_DIR:-$(pwd)}"
# Honor any externally-set TICKETS_DIR (the agent's env normally pins it
# via .mngr/settings.toml -- e.g. /code/runtime/tickets -- so the tk
# tickets live alongside the runtime-backup branch). Fall back to tk's
# unset-default of <repo>/.tickets when nothing is set.
tickets_dir="${TICKETS_DIR:-${repo_root}/.tickets}"

# Drain stdin.
cat > /dev/null

[[ -d "$tickets_dir" ]] || exit 0

tk_script="${repo_root}/vendor/tk/ticket"
[[ -x "$tk_script" ]] || exit 0

# Re-export so tk picks up the resolved value even when this hook runs
# from outside the repo root (which would otherwise trigger tk's
# parent-walk and potentially land on a random ancestor).
export TICKETS_DIR="$tickets_dir"

# `tk steps` lists only step records (creator-scoped to $MNGR_AGENT_NAME
# when set, so a sibling agent's steps never leak into this agent's
# reminder). Output format: <id>  [<status>] - <title>
open_lines=$("$tk_script" steps 2>/dev/null | sed '/^[[:space:]]*$/d' || true)

[[ -n "$open_lines" ]] || exit 0

cat <<EOF

[Open task reminder from forever-claude-template]

You have step records that are not yet closed:

$open_lines

For each one, decide before continuing: keep working on it (call \`tk start <id>\` if it's not already in_progress), replace it with a fresh step, or close it now with \`tk close <id> "<summary>"\` (the positional summary is required for steps). The summary is a concise one-line description of the *work done* in this step (the caption a non-technical user sees), not the outcome -- the outcome goes in your final assistant message. Steps are sequential: do not start a new step until the previous one is closed.

See CLAUDE.md > Task management for the full protocol.
EOF
