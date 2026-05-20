#!/usr/bin/env bash
# Stop hook: NON-BLOCKING reminder if the agent stops while tickets are still
# open or in_progress. Exits 0 always so the agent is never re-engaged --
# carryover into the next turn (driven by the UserPromptSubmit reminder)
# handles real follow-up. This stderr message is mainly for orchestrator log
# / human visibility.
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

# `tk steps` lists this agent's open step records only (regular tickets
# are managed cross-agent and aren't part of the per-turn progress flow
# the chat view renders).
open_count=$("$tk_script" steps 2>/dev/null | sed '/^[[:space:]]*$/d' | wc -l | tr -d ' ')

if [[ "${open_count:-0}" -gt 0 ]]; then
    echo "[task-management] Stopping with ${open_count} step record(s) still open. They'll appear at the top of the next turn's progress block." >&2
fi
exit 0
