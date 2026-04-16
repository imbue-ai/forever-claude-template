#!/usr/bin/env bash
set -euo pipefail

# Claude agent provisioning hook. Runs once via `extra_provision_command`
# when an agent of type `claude` (or a subtype) is created from this
# template. Prepares the work directory so Claude Code sees its expected
# CLAUDE.md alongside the canonical AGENTS.md.

WORKDIR="${MNGR_AGENT_WORK_DIR:-$(pwd)}"
cd "$WORKDIR"

# Claude Code reads CLAUDE.md, not AGENTS.md. Copy the canonical instructions
# so both filenames point at the same content. Using `cp -f` so that re-running
# this script against an already-provisioned work dir overwrites a stale copy.
cp -f AGENTS.md CLAUDE.md
