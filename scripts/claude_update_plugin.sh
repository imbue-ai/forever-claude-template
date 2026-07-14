#!/usr/bin/env bash
set -euo pipefail

# Plugins to install/update synchronously on session start, so they are
# available immediately rather than relying on Claude Code's lazy auto-install.
PLUGIN_IDS=(
    "imbue-code-guardian@imbue-code-guardian"
    "frontend-design@claude-code-plugins"
)

# Check if claude CLI is available
if ! command -v claude &>/dev/null; then
    exit 0
fi

# Clear stale plugin cache for our marketplaces to avoid using outdated agents/skills
CACHE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/plugins/cache"
rm -rf "$CACHE_DIR/imbue-mngr" "$CACHE_DIR/imbue-code-guardian" "$CACHE_DIR/claude-code-plugins" 2>/dev/null || true

# The plugins and marketplaces are configured at project scope in
# .claude/settings.json (extraKnownMarketplaces + enabledPlugins).
# Install with --scope project (a no-op if already present) so each plugin is
# available synchronously rather than via Claude Code's lazy auto-install, then
# update to pull the latest version.
#
# The scope MUST be project. `claude plugin install` defaults to *user* scope,
# which enables the plugin for every Claude session on the host -- including
# headless `claude -p` children that pass `--setting-sources user` (e.g.
# pr-review's dependency-install agent). A user-scoped code-guardian would then
# run its review/CI Stop hook against those children and block them. Keeping the
# plugins project-scoped confines them to this repo's own agent.
for plugin_id in "${PLUGIN_IDS[@]}"; do
    claude plugin install --scope project "$plugin_id" 2>/dev/null || true
    claude plugin update --scope project "$plugin_id" 2>/dev/null || true
done
