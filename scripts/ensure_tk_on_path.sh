#!/usr/bin/env bash
# SessionStart hook: symlink the vendored tk script into ~/.local/bin so the
# agent can call `tk` (or `ticket`) without typing the full path. Idempotent.
# Future Docker builds bake this in via the Dockerfile; this hook covers
# already-built images and local dev sessions.
set -euo pipefail

repo_root="${MNGR_AGENT_WORK_DIR:-$(pwd)}"
tk_script="${repo_root}/vendor/tk/ticket"

[[ -x "$tk_script" ]] || exit 0

mkdir -p "${HOME}/.local/bin"

for name in tk ticket; do
    target="${HOME}/.local/bin/${name}"
    if [[ -L "$target" ]] && [[ "$(readlink "$target")" == "$tk_script" ]]; then
        continue
    fi
    ln -sf "$tk_script" "$target"
done
