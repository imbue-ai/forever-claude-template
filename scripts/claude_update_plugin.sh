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
# Install (a no-op if already present) so each plugin is available
# synchronously rather than via Claude Code's lazy auto-install, then
# update to pull the latest version.
for plugin_id in "${PLUGIN_IDS[@]}"; do
    claude plugin install "$plugin_id" 2>/dev/null || true
    claude plugin update "$plugin_id" 2>/dev/null || true
done
