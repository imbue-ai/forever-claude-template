# forever-claude-template

A template for running a persistent instance of Claude Code that communicates via Telegram.

## Usage

```bash
mngr create my-agent forever-claude \
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
- `SOUL.md` - Personality
- `skills/` - Agent skills
- `scripts/` - Utility scripts (wait, stop hook)
- `services.toml` - Background services
- `libs/telegram_bot/` - Telegram bot and send CLI
- `libs/bootstrap/` - Service manager
