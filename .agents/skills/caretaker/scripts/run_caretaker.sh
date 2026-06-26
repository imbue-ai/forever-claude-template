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
# recreated fresh on every run: mngr destroys the previous Caretaker agent and
# creates a new one. A brand-new agent always starts from an empty chat, which
# is the only reliable way to clear the conversation -- an in-session "/clear"
# starts a new Claude session id that the system interface is not watching, so
# it does not actually clear the rendered chat.
#
# Two branches, gated on the persistent `introduced` preference (set true once,
# right after the very first welcome is delivered, so the welcome never reappears
# even if the agent is later destroyed and recreated):
#   - introduced == false -> first ever run: create with /caretaker-welcome (the
#                            fixed, pre-prepared greeting), then mark introduced.
#   - introduced == true  -> retire the old Caretaker and create a fresh one that
#                            just runs the caretaker skill, informed by the
#                            preferences the user gave in response to the welcome.
set -euo pipefail

CARETAKER_NAME="caretaker"
CARETAKER_FILTER='labels.caretaker == "true"'
PREFERENCES_SCRIPT=".agents/skills/caretaker/scripts/preferences.py"

# Sent on the very first run only. Mirrors how the initial chat is created
# (mngr create ... --message /welcome): the Caretaker's first message is the
# /caretaker-welcome slash command, which emits a fixed, pre-prepared welcome
# verbatim and runs no routine -- so the very first thing the user sees is the
# welcome, delivered the same way as the main chat's, not an agent-improvised run.
WELCOME_COMMAND="/caretaker-welcome"

# Sent to a freshly-created Caretaker on every later run: just run the skill,
# which reads the user's recorded preferences and acts accordingly.
RUN_MESSAGE="It's time for your *caretaking* run. Follow your caretaker skill (.agents/skills/caretaker/SKILL.md)."

# Sent to a still-running Caretaker so it finishes its run log gracefully before
# we retire it for the new day.
WRAPUP_MESSAGE="A new day's caretaking run is due while you are still mid-run. Please finish writing your current run log now and stop; I will start your fresh run for the new day."

# How long (seconds) to let a mid-run Caretaker finish its log after the wrap-up
# nudge before we destroy it anyway.
WRAPUP_GRACE_SECONDS=60

log() { printf '%s run_caretaker: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

preference() { uv run python "$PREFERENCES_SCRIPT" "$@"; }

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

# Create a fresh Caretaker whose first message is "$1" (the welcome command on
# the first run, the run nudge thereafter). A brand-new agent starts from an
# empty chat, so this is what clears the conversation for each run.
create_caretaker() {
  local initial_message="$1" workspace label_args=()
  workspace="$(resolve_workspace)"
  if [ -n "$workspace" ]; then
    label_args=(--label "workspace=${workspace}")
  fi
  log "creating a fresh Caretaker (first message: ${initial_message})"
  uv run mngr create "$CARETAKER_NAME" \
    --transfer none \
    --template caretaker \
    --no-connect \
    --format json \
    --label caretaker=true \
    --label auto_created=true \
    --label "caretaker_run=$(date +%s)" \
    "${label_args[@]}" \
    --message "$initial_message"
}

# Retire every existing Caretaker so the next create starts from a clean slate.
# A running Caretaker is first asked to finish its log (bounded wait), then every
# Caretaker is destroyed (``--force`` stops any still running). Best-effort: a
# destroy failure self-heals on the next run, since the singleton filter would
# pick up the straggler and retire it then.
retire_caretakers() {
  local ids="$1" id running waited
  running="$(running_caretaker_ids)"
  if [ -n "${running//[[:space:]]/}" ]; then
    while IFS= read -r id; do
      [ -n "$id" ] || continue
      log "Caretaker ${id} is mid-run; asking it to finish its log"
      uv run mngr message "$id" --message "$WRAPUP_MESSAGE" 2>/dev/null || true
    done <<<"$running"
    waited=0
    while [ "$waited" -lt "$WRAPUP_GRACE_SECONDS" ]; do
      running="$(running_caretaker_ids)"
      [ -z "${running//[[:space:]]/}" ] && break
      sleep 3
      waited=$((waited + 3))
    done
  fi
  while IFS= read -r id; do
    [ -n "$id" ] || continue
    log "retiring old Caretaker ${id}"
    uv run mngr destroy "$id" --force 2>/dev/null || log "could not destroy ${id} (will retry next run)"
  done <<<"$ids"
}

main() {
  local introduced ids
  introduced="$(preference get introduced 2>/dev/null || echo false)"
  ids="$(caretaker_ids)"

  if [ "$introduced" != "true" ]; then
    # Very first run ever: deliver the welcome. Clear out any stray Caretaker
    # first so we start from a single, fresh agent.
    if [ -n "${ids//[[:space:]]/}" ]; then
      retire_caretakers "$ids"
    fi
    create_caretaker "$WELCOME_COMMAND"
    preference set introduced true
    log "first-run welcome delivered; introduced=true"
    return 0
  fi

  # Every later run: replace the old Caretaker with a fresh one (clean chat) that
  # just runs the skill.
  if [ -n "${ids//[[:space:]]/}" ]; then
    retire_caretakers "$ids"
  fi
  create_caretaker "$RUN_MESSAGE"
  log "fresh Caretaker created for this run"
}

main "$@"
