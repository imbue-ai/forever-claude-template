#!/usr/bin/env bash
#
# run_caretaker.sh -- wake the singleton Caretaker agent for its nightly run.
#
# Invoked by the scheduler service (the `caretaker` task in
# runtime/scheduled_tasks.toml) once a night. Runs from the repo root
# (/mngr/code), in the services agent's environment (MNGR_HOST_DIR,
# MNGR_AGENT_ID, ... are inherited).
#
# The Caretaker is a singleton, identified by its `caretaker=true` label.
# Three branches:
#   - none exists           -> `mngr create` it (so it first appears on day 2)
#   - exists, idle          -> clear its chat, then message it to run
#   - exists, busy (RUNNING) -> ask it to finish its log, then clear + run
#
# mngr -- not the agent -- drives the clear. An agent that writes "/clear" in
# its own response only emits text; the slash command only fires when it is
# sent to the agent's stdin (exactly as a user typing "/clear" would). So each
# re-wake first sends "/clear" via mngr to wipe the prior conversation, then
# sends the run trigger, so every night starts from a clean chat. A freshly
# created Caretaker has nothing to clear, so creation skips the /clear and the
# first thing the user ever sees is the Caretaker's welcome message.
set -euo pipefail

CARETAKER_NAME="caretaker"
CARETAKER_FILTER='labels.caretaker == "true"'

# Sent on first creation only: the very first thing the user ever sees from the
# Caretaker should be its welcome, not the recurring "caretaking run" nudge (that
# appears from the second day onward). A fresh agent has no prior context to
# clear, so this just triggers the skill, whose first-run path's entire chat
# output is the pre-prepared welcome message.
FIRST_RUN_MESSAGE="Please introduce yourself to the user by following your caretaker skill (.agents/skills/caretaker/SKILL.md)."

# Sent (after a "/clear") to re-wake an existing Caretaker for a fresh run.
RUN_MESSAGE="It's time for your *caretaking* run. Follow your caretaker skill (.agents/skills/caretaker/SKILL.md)."

# Sent to a busy Caretaker so it finishes gracefully; the actual clear + run for
# the new day is then driven by mngr (clear_and_run below) on the next branch.
WRAPUP_MESSAGE="A new day's caretaking run is due while you are still mid-run. Please finish writing your current run log now and stop; I will start your fresh run for the new day."

# Short pause between the "/clear" send and the run send. send_message already
# blocks until the TUI is ready before each paste, so the run message cannot be
# pasted until the agent is idle again after processing "/clear"; this sleep is
# belt-and-suspenders, since "/clear" is a near-instant local operation that may
# leave the readiness gate satisfied within the same frame.
CLEAR_SETTLE_SECONDS=2

log() { printf '%s run_caretaker: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# Resolve the workspace label so the Caretaker tab groups with the user's other
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

# Active Caretaker agent ids (one per line; empty if none).
caretaker_ids() {
  uv run mngr list --active --include "$CARETAKER_FILTER" --ids --on-error continue 2>/dev/null || true
}

# Ids of Caretaker agents currently RUNNING (mid-turn). `--running` is mngr's
# alias for `--include 'state == "RUNNING"'`, so we never parse state strings.
running_caretaker_ids() {
  uv run mngr list --active --running --include "$CARETAKER_FILTER" --ids --on-error continue 2>/dev/null || true
}

create_caretaker() {
  local workspace label_args=()
  workspace="$(resolve_workspace)"
  if [ -n "$workspace" ]; then
    label_args=(--label "workspace=${workspace}")
  fi
  log "no Caretaker found; creating one"
  uv run mngr create "$CARETAKER_NAME" \
    --transfer none \
    --template caretaker \
    --no-connect \
    --format json \
    --label caretaker=true \
    --label auto_created=true \
    "${label_args[@]}" \
    --message "$FIRST_RUN_MESSAGE"
}

# Re-wake an existing Caretaker: mngr sends "/clear" to wipe the prior chat,
# then (after a short settle) sends the run trigger. `--start` ensures a stopped
# agent is started before each send.
clear_and_run() {
  local agent_id="$1"
  log "clearing Caretaker ${agent_id}'s chat"
  uv run mngr message "$agent_id" --start --message "/clear"
  sleep "$CLEAR_SETTLE_SECONDS"
  log "sending fresh caretaking run message to ${agent_id}"
  uv run mngr message "$agent_id" --start --message "$RUN_MESSAGE"
}

main() {
  local ids first_id running
  ids="$(caretaker_ids)"

  if [ -z "${ids//[[:space:]]/}" ]; then
    create_caretaker
    log "Caretaker created"
    return 0
  fi

  first_id="$(printf '%s\n' "$ids" | head -n1)"
  running="$(running_caretaker_ids)"

  if printf '%s\n' "$running" | grep -qxF "$first_id"; then
    log "Caretaker ${first_id} is mid-run; sending graceful wrap-up before restarting"
    uv run mngr message "$first_id" --message "$WRAPUP_MESSAGE"
  fi

  clear_and_run "$first_id"
}

main "$@"
