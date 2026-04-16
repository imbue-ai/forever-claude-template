#!/usr/bin/env bash
set -euo pipefail

# Hermes agent provisioning hook. Runs once via `extra_provision_command`
# when an agent of type `hermes` (or a subtype) is created from this
# template. Overlays the template's hermes config + plugins on top of
# whatever the mngr_hermes plugin already seeded into HERMES_HOME.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKDIR="${MNGR_AGENT_WORK_DIR:-$(pwd)}"
cd "$WORKDIR"

: "${HERMES_HOME:?HERMES_HOME must be set by the mngr_hermes plugin before setup runs}"

mkdir -p "$HERMES_HOME/plugins"

# Merge template config overrides (model, toolsets, external_skill_dirs)
# on top of the user's seeded ~/.hermes config. Preserves provider
# endpoints, API settings, and any other user overrides.
uv run --no-project "$SCRIPT_DIR/merge_config.py" \
    --base "$HERMES_HOME/config.yaml" \
    --override "$SCRIPT_DIR/config.yaml" \
    --output "$HERMES_HOME/config.yaml"

# Copy template plugins into HERMES_HOME/plugins/. Hermes discovers
# plugins under HERMES_HOME/plugins/<name>/ at session start.
cp -R "$SCRIPT_DIR/plugins/." "$HERMES_HOME/plugins/"
