#!/usr/bin/env bash
set -euo pipefail

# Verifies the agent's current working directory is the repo root.
# Exits 2 (blocking) with a reminder message if it is not, so the agent is
# prompted to cd back to the root before finishing.

if [ ! -e .git ]; then
    echo "Be sure to return to the repo root when you finish! Otherwise the other stop hooks cannot run correctly." >&2
    exit 2
fi

exit 0
