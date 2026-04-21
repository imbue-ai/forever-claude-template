#!/usr/bin/env bash
set -euo pipefail

# Copy the bundled worker sub-skills from this skill's assets/ directory into a
# staging location that the crystallize-worker create template consumes at
# provision time (see .mngr/settings.toml).
#
# Usage:
#   install_worker_skills.sh <destination-directory>
#
# The destination is created if missing. Existing contents with matching names
# are overwritten so the worker always gets the freshest copy.

if [ $# -ne 1 ]; then
    echo "usage: $0 <destination-directory>" >&2
    exit 2
fi

destination="$1"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source_root="$script_dir/../assets/worker-skills"

if [ ! -d "$source_root" ]; then
    echo "expected worker-skills source at $source_root" >&2
    exit 1
fi

mkdir -p "$destination"
# Copy each sub-skill directory wholesale.
for entry in "$source_root"/*; do
    [ -d "$entry" ] || continue
    name="$(basename "$entry")"
    rm -rf "${destination:?}/$name"
    cp -R "$entry" "$destination/$name"
done

echo "installed worker sub-skills into $destination"
