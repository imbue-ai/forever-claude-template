- Enabled Claude Code fast mode for all agents created from this repo by
  setting `fastMode = true` in the `settings_overrides__extend` for
  `[agent_types.claude]` in `.mngr/settings.toml` (was `false`). Because
  `settings_overrides` is applied last during mngr's Claude provisioning, this
  forces fast mode on for every agent type (claude/main/worker/chat/worktree),
  not just attended local ones. The `CLAUDE_CODE_ENABLE_OPUS_4_7_FAST_MODE=1`
  host env var that gates the capability was already present.

- Added `CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK=1` to `host_env__extend` in
  `.mngr/settings.toml`. Agents authenticate with a Claude Max subscription that
  supports fast mode, but the session resolves under an organization whose
  org-level fast-mode check otherwise reports fast mode as "currently
  unavailable" -- so without this, `/fast` was refused inside the container even
  though the same account gets fast mode on the host machine. This env var
  bypasses that org check so fast mode actually works for in-container agents.
