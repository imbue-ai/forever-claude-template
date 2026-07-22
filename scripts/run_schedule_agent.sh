#!/usr/bin/env bash
#
# run_schedule_agent.sh -- wake a singleton "schedule agent" for one scheduled run.
#
# A schedule agent is a once-per-cadence agent that runs a single skill: it is
# created once, kept alive across runs (never destroyed/recreated), and on each
# run has its chat cleared and its skill re-triggered in place. The weekly
# Caretaker is the canonical example (`run_schedule_agent.sh caretaker`), but the same
# machinery drives any skill on any cadence -- e.g. a morning news agent
# (`run_schedule_agent.sh news`) -- so adding one is just: write a skill and schedule
# this script. See the manage-scheduled-tasks skill.
#
# Usage:
#   run_schedule_agent.sh <skill> [--template <template>] [--agent-name <name>]
#
#   <skill>          Required. Names the skill to run: the agent is messaged
#                    `/<skill>` on every run and found as a singleton by the
#                    `schedule_agent=<skill>` label.
#   --template <t>   Create template for the agent (default: `schedule_agent`, a plain
#                    claude agent oriented to run the named skill). The Caretaker
#                    passes its own tailored template (`caretaker`).
#   --agent-name <n> Agent name shown in the UI (default: the skill name).
#
# Invoked by the weekly caretaker job (the /etc/cron.d/minds-caretaker entry
# the enable-caretaker skill creates, via scripts/run_daily_job.sh and
# scripts/caretaker_check.sh) or any other cron entry, through
# scripts/with_agent_env.sh so it runs from the repo root (/mngr/code) with the
# services agent's environment (MNGR_HOST_DIR, MNGR_AGENT_ID, ... -- cron
# scrubs the env otherwise).
#
# On each run:
#   - No agent yet (first run ever) -> create the persistent agent whose first
#     message is `/<skill>`. A brand-new agent starts from an empty chat, so a
#     self-detecting skill (like caretaker) can deliver a first-run welcome.
#   - Agent already exists -> send `/clear` to start a fresh session, then
#     send `/<skill>` to run again with a clean context.
#
# `/clear` starts a new session, so the skill re-runs with no memory of the
# previous run -- it re-detects first-run state, re-reads its own files, etc.
# The clear resets the agent's context (what the next run reasons from), which
# is what makes each run genuinely fresh.
set -euo pipefail

# ---- Arguments --------------------------------------------------------------
SKILL=""
TEMPLATE="schedule_agent"
AGENT_NAME=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --template) TEMPLATE="$2"; shift 2 ;;
    --agent-name) AGENT_NAME="$2"; shift 2 ;;
    --*) echo "run_schedule_agent: unknown option: $1" >&2; exit 2 ;;
    *)
      if [ -z "$SKILL" ]; then SKILL="$1"; shift
      else echo "run_schedule_agent: unexpected argument: $1" >&2; exit 2; fi
      ;;
  esac
done
if [ -z "$SKILL" ]; then
  echo "usage: run_schedule_agent.sh <skill> [--template <template>] [--agent-name <name>]" >&2
  exit 2
fi
AGENT_NAME="${AGENT_NAME:-$SKILL}"

# Singleton identity + the per-run trigger. The run message is a hidden
# slash-command (like /welcome), so the user's first visible message is always
# the agent's own output, never the command that produced it.
SCHEDULE_FILTER="labels.schedule_agent == \"${SKILL}\""
RUN_MESSAGE="/${SKILL}"

# Settle time (seconds) between sending /clear and the run trigger, so the clear
# lands (the fresh session starts) before the run trigger.
CLEAR_SETTLE_SECONDS=2

log() { printf '%s run_schedule_agent[%s]: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$SKILL" "$*"; }

# Resolve the workspace label so the agent's tab groups with the user's other
# agents in the minds UI (mirrors libs/bootstrap's create-chat workspace logic:
# prefer the services agent's `workspace` label, fall back to the host_name).
resolve_workspace() {
  python3 - <<'PY'
import json, os, sys

host_dir = os.environ.get("MNGR_HOST_DIR", "")
agent_id = os.environ.get("MNGR_AGENT_ID", "")
if host_dir and agent_id:
    try:
        with open(os.path.join(host_dir, "agents", agent_id, "data.json")) as handle:
            workspace = json.load(handle).get("labels", {}).get("workspace")
        if workspace:
            print(workspace)
            sys.exit(0)
    except (OSError, ValueError):
        pass
if host_dir:
    try:
        with open(os.path.join(host_dir, "data.json")) as handle:
            print(json.load(handle).get("host_name", ""))
            sys.exit(0)
    except (OSError, ValueError):
        pass
print("")
PY
}

# Active schedule-agent ids for this skill (one per line; empty if none).
schedule_agent_ids() {
  uv run mngr list --active --include "$SCHEDULE_FILTER" --ids --on-error continue 2>/dev/null || true
}

# Create the persistent schedule agent whose first message is `/<skill>`. A brand-new
# agent starts from an empty chat, so a self-detecting skill delivers its
# first-run behavior (e.g. the caretaker's welcome) on this first run.
create_schedule_agent() {
  local workspace label_args=()
  workspace="$(resolve_workspace)"
  if [ -n "$workspace" ]; then
    label_args=(--label "workspace=${workspace}")
  fi
  log "creating the persistent schedule agent (template: ${TEMPLATE}, first message: ${RUN_MESSAGE})"
  uv run mngr create "$AGENT_NAME" \
    --transfer none \
    --template "$TEMPLATE" \
    --no-connect \
    --format json \
    --label "schedule_agent=${SKILL}" \
    "${label_args[@]}" \
    --message "$RUN_MESSAGE"
}

main() {
  local ids id
  ids="$(schedule_agent_ids)"

  if [ -z "${ids//[[:space:]]/}" ]; then
    # First run ever: no agent exists, so create the persistent one.
    create_schedule_agent
    log "persistent schedule agent created; first run started"
    return 0
  fi

  # Agent already exists: keep it, clear its chat, and re-trigger in place.
  # Preserve the singleton invariant by operating on the first id if (unexpectedly)
  # more than one exists.
  id="$(printf '%s\n' "$ids" | head -n 1)"

  # Clear the rendered chat so this run starts from an empty conversation.
  log "clearing schedule agent ${id} for a fresh run"
  uv run mngr message "$id" --start --message "/clear"

  # Let the clear land (new session boundary recorded) before triggering the run.
  sleep "$CLEAR_SETTLE_SECONDS"

  # Re-trigger the skill in the now-empty chat.
  log "triggering schedule agent ${id} run"
  uv run mngr message "$id" --start --message "$RUN_MESSAGE"
}

main
