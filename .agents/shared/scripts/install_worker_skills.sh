#!/usr/bin/env bash
set -euo pipefail

# Install bundled worker sub-skills from each parent skill's assets/worker/
# directory into the worker's .agents/skills/ tree. Called at worker provision
# time by the crystallize-worker create template (see .mngr/settings.toml).
#
# Each parent skill under .agents/skills/ that has an assets/worker/ directory
# is installed at <destination>/<parent>-worker/ so it becomes loadable as a
# regular skill inside the worker.
#
# Usage:
#   install_worker_skills.sh <destination-directory>
#
# The destination is created if missing. Existing <parent>-worker/ dirs are
# overwritten so the worker always gets the freshest copy.

if [ $# -ne 1 ]; then
    echo "usage: $0 <destination-directory>" >&2
    exit 2
fi

destination="$1"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# script lives at <repo>/.agents/shared/scripts; walk up three to reach repo root
repo_root="$(cd "$script_dir/../../.." && pwd)"
skills_root="$repo_root/.agents/skills"

if [ ! -d "$skills_root" ]; then
    echo "expected skills root at $skills_root" >&2
    exit 1
fi

mkdir -p "$destination"
installed_count=0
for parent_dir in "$skills_root"/*; do
    worker_source="$parent_dir/assets/worker"
    [ -d "$worker_source" ] || continue
    parent_name="$(basename "$parent_dir")"
    dest_name="${parent_name}-worker"
    rm -rf "${destination:?}/$dest_name"
    cp -R "$worker_source" "$destination/$dest_name"
    installed_count=$((installed_count + 1))
done

if [ "$installed_count" -eq 0 ]; then
    echo "no parent skills with assets/worker/ found under $skills_root" >&2
    exit 1
fi

echo "installed $installed_count worker sub-skills into $destination"
