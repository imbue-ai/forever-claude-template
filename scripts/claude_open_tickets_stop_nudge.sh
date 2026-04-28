#!/usr/bin/env bash
# Stop hook: NON-BLOCKING reminder if the agent stops while tickets are still
# open or in_progress. Exits 0 always so the agent is never re-engaged --
# carryover into the next turn (driven by the UserPromptSubmit reminder)
# handles real follow-up. This stderr message is mainly for orchestrator log
# / human visibility.
set -euo pipefail

repo_root="${MNGR_AGENT_WORK_DIR:-$(pwd)}"
tickets_dir="${repo_root}/.tickets"

# Drain stdin.
cat > /dev/null

[[ -d "$tickets_dir" ]] || exit 0

tk_script="${repo_root}/vendor/tk/ticket"
[[ -x "$tk_script" ]] || exit 0

# tk's parent-walk for .tickets/ would otherwise fall back to a random
# ancestor when this hook runs from outside the repo root.
export TICKETS_DIR="$tickets_dir"

# `tk ready` is a built-in that lists open + in_progress tickets (with deps
# resolved -- and we don't use deps).
open_count=$("$tk_script" ready 2>/dev/null | sed '/^[[:space:]]*$/d' | wc -l | tr -d ' ')

if [[ "${open_count:-0}" -gt 0 ]]; then
    echo "[task-management] Stopping with ${open_count} ticket(s) still open. They'll appear at the top of the next turn's progress block." >&2
fi
exit 0
