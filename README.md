# forever-claude-template

A self-contained template for running a persistent Claude agent that communicates via Telegram, delegates work to sub-agents, and can manage its own background services.

## Usage

```bash
mngr create my-workspace main -t local \
    --host-env MINDS_WORKSPACE_NAME=my-workspace \
    --project ~/project/forever-claude-template \
    --pass-env TELEGRAM_BOT_TOKEN \
    --pass-env TELEGRAM_USER_NAME
```

## Structure

- `AGENTS.md` - Agent instructions (copied to `CLAUDE.md` at provisioning for claude agents)
- `parent.toml` - Upstream repo for pulling updates
- `.mngr/settings.toml` - Agent types, create templates, command defaults
- `skills/` - Agent skills (telegram, task delegation, services, self-update)
- `scripts/` - Utility scripts (reviewer settings)
- `event-processor/` - Pre-configured directory for creating persistent sub-agents
- `services.toml` - Background services managed by bootstrap
- `libs/telegram_bot/` - Telegram bot, send CLI, and history viewer
- `libs/bootstrap/` - Service manager (reconciles services.toml with tmux windows)
- `vendor/mngr/` - A vendored, mutable copy of mngr. Note that making changes here *will* affect the behavior of the `mngr` command

## Create templates

- `worker` - For sub-agents created via the launch-task skill (includes code review)
