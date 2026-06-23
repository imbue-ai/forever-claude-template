#!/usr/bin/env bash
set -euo pipefail

# Install the generic harden worker sub-skill into a worker's .agents/skills/
# tree. Called at worker provision time by the subskill-worker create template
# (see .mngr/settings.toml).
#
# There is exactly one worker source -- the generic worker at
# .agents/shared/worker/. It is installed at <destination>/harden-worker/ so it
# becomes loadable as a regular skill inside the worker. Homing it under
# .agents/shared/ (rather than under a parent skill's assets/worker/) keeps any
# worker-only material out of the auto-loaded .agents/skills/ tree, and means
# every subskill-worker installs exactly this one worker -- it reads the
# operation + artifact from its task file and composes the matching references.
#
# Usage:
#   install_worker_skills.sh <destination-directory>
#
# The destination is created if missing. An existing harden-worker/ dir is
# overwritten so the worker always gets the freshest copy.

if [ $# -ne 1 ]; then
    echo "usage: $0 <destination-directory>" >&2
    exit 2
fi

destination="$1"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# script lives at <repo>/.agents/shared/scripts; walk up one to reach .agents/shared
worker_source="$(cd "$script_dir/.." && pwd)/worker"

if [ ! -d "$worker_source" ]; then
    echo "expected generic worker at $worker_source" >&2
    exit 1
fi

mkdir -p "$destination"
dest_name="harden-worker"
rm -rf "${destination:?}/$dest_name"
cp -R "$worker_source" "$destination/$dest_name"

echo "installed harden-worker into $destination"
