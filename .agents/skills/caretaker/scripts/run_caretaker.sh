#!/usr/bin/env bash
#
# run_caretaker.sh -- entry point for the nightly Caretaker run.
#
# The Caretaker is one example of a "task agent": a singleton skill run on a
# cadence. The shared machinery lives in scripts/run_task_agent.sh; this wrapper
# just invokes it for the `caretaker` skill with the Caretaker's tailored
# template. A new task agent (e.g. a morning news digest) doesn't need a wrapper
# like this -- it schedules `scripts/run_task_agent.sh <skill>` directly. See the
# manage-scheduled-tasks skill.
set -euo pipefail

# Resolve the repo root from this script's location so it works regardless of the
# caller's cwd (the scheduler runs from /mngr/code; direct callers may not).
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"

exec bash "$REPO_ROOT/scripts/run_task_agent.sh" caretaker --template caretaker "$@"
