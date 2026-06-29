#!/usr/bin/env bash
#
# run_caretaker.sh -- wake the singleton Caretaker agent for its nightly run.
#
# Invoked by the scheduler service (the `caretaker` task in
# runtime/scheduled_tasks.toml) once a night. Runs from the repo root
# (/mngr/code), in the services agent's environment (MNGR_HOST_DIR,
# MNGR_AGENT_ID, ... are inherited).
#
# The Caretaker is a singleton, identified by its `caretaker=true` label, and is
# PERSISTENT: it is created once and then kept alive across every run -- never
# destroyed and recreated. Each run clears its chat and re-triggers its routine
# in place:
#   - No Caretaker yet (first run ever) -> create one whose first message is
#     `/caretaker`. The caretaker skill is idempotent: on an empty chat it
#     self-detects the first run and delivers the welcome.
#   - Caretaker already exists -> bump its run key (so the minds UI re-surfaces
#     and re-flashes the tab if the user had closed it), send `/clear` to wipe
#     the rendered chat, then send `/caretaker` to run the routine again. The
#     skill self-detects that this is a later run and runs the routine rather
#     than the welcome.
#
# `/clear` actually clears the rendered chat: the system interface renders only
# the sessions at or after the most recent `/clear` boundary in the agent's
# session history, so a clear makes the previous run's transcript disappear and
# the new run starts from an empty chat.
set -euo pipefail

CARETAKER_NAME="caretaker"
CARETAKER_FILTER='labels.caretaker == "true"'

# The single trigger, sent on EVERY run (first and later). The caretaker skill is
# idempotent and self-detecting: on an empty/first-run chat it delivers the
# welcome, otherwise it runs the nightly routine. Like /welcome it is hidden in
# the chat UI, so the user's first visible message is always the Caretaker's own
# output, never the slash command that produced it.
RUN_MESSAGE="/caretaker"

# Settle time (seconds) between sending /clear and sending /caretaker, so the
# clear lands (and the new session boundary is recorded) before the run starts.
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

# Create the persistent Caretaker whose first message is `/caretaker`. A
# brand-new agent starts from an empty chat, so the idempotent skill delivers
# the welcome on this first run.
create_caretaker() {
  local workspace label_args=()
  workspace="$(resolve_workspace)"
  if [ -n "$workspace" ]; then
    label_args=(--label "workspace=${workspace}")
  fi
  log "creating the persistent Caretaker (first message: ${RUN_MESSAGE})"
  uv run mngr create "$CARETAKER_NAME" \
    --transfer none \
    --template caretaker \
    --no-connect \
    --format json \
    --label caretaker=true \
    --label "highlight=$(date +%s)" \
    "${label_args[@]}" \
    --message "$RUN_MESSAGE"
}

main() {
  local ids id
  ids="$(caretaker_ids)"

  if [ -z "${ids//[[:space:]]/}" ]; then
    # First run ever: no Caretaker exists, so create the persistent one.
    create_caretaker
    log "persistent Caretaker created; first run started"
    return 0
  fi

  # Caretaker already exists: keep it, clear its chat, and re-trigger in place.
  # Preserve the singleton invariant by operating on the first id if (unexpectedly)
  # more than one exists.
  id="$(printf '%s\n' "$ids" | head -n 1)"

  # Bump the highlight key so the minds UI re-flashes the tab for this new run --
  # whether the user had closed it (re-surfaced) or left it open in the background
  # (re-blinked in place).
  uv run mngr label "$id" -l "highlight=$(date +%s)" 2>/dev/null || true

  # Clear the rendered chat so this run starts from an empty conversation.
  log "clearing Caretaker ${id} for a fresh run"
  uv run mngr message "$id" --start --message "/clear"

  # Let the clear land (new session boundary recorded) before triggering the run.
  sleep "$CLEAR_SETTLE_SECONDS"

  # Re-trigger the (idempotent) caretaker routine in the now-empty chat.
  log "triggering Caretaker ${id} run"
  uv run mngr message "$id" --start --message "$RUN_MESSAGE"
}

main "$@"
