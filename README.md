# forever-claude-template

A self-contained template for running a persistent Claude agent that communicates via Telegram, delegates work to sub-agents, and can manage its own background services.

## Usage

```bash
mngr create my-mind main -t local \
    --host-env MIND_NAME=my-mind \
    --project ~/project/forever-claude-template \
    --pass-env TELEGRAM_BOT_TOKEN \
    --pass-env TELEGRAM_USER_NAME
```

## Setup

1. Create a Telegram bot via [@BotFather](https://t.me/botfather) and get the token
2. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_USER_NAME` in your environment
3. Run `uv sync` in this repo to install dependencies
4. Create the agent with `mngr create`

The agent will start, read its PURPOSE.md (which asks it to figure out what to do), and message you on Telegram.

## Structure

- `CLAUDE.md` - Agent instructions
- `PURPOSE.md` - Current purpose (agent modifies this)
- `SOUL.md` - Personality and values
- `parent.toml` - Upstream repo for pulling updates
- `.mngr/settings.toml` - Agent types, create templates, command defaults
- `skills/` - Agent skills (telegram, task delegation, services, self-update)
- `scripts/` - Utility scripts (reviewer settings)
- `event-processor/` - Pre-configured directory for creating persistent sub-agents
- `services.toml` - Background services managed by bootstrap
- `libs/telegram_bot/` - Telegram bot, send CLI, and history viewer
- `libs/bootstrap/` - Service manager (reconciles services.toml with tmux windows)

## Create templates

- `local` - Run locally with bootstrap service manager
- `modal` - Run on Modal with bootstrap and GitHub setup
- `docker` - Run in Docker with bootstrap
- `worker` - For sub-agents created via the launch-task skill (includes code review)
