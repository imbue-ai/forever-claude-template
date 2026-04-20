#!/usr/bin/env bash
set -euo pipefail

# Common per-session agent setup. Runs for both claude and hermes agents via
# each agent type's session-start hook.

uv sync --all-packages
