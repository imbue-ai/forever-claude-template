---
name: dealing-with-the-unexpected
description: Handle unexpected situations where things are not working as expected. Use when you encounter errors, confusing state, or behavior that contradicts your docs and prompts.
---

# Dealing with the unexpected

When something unexpected happens, follow this procedure:

## 1. Gather information

Before doing anything, understand what is actually happening:

- Read any error messages carefully
- Check the tmux windows: `tmux list-windows -t $(tmux display-message -p '#S')`
- Check if services are running: look at `services.toml` vs actual tmux windows
- Check recent telegram history: `tail -20 .runtime/telegram/history.jsonl`
- Check the wait counter state: `cat .runtime/wait_counter 2>/dev/null || echo "no counter"`

## 2. Diagnose

Common issues and their causes:

- **Telegram bot not delivering messages**: Check the `telegram` tmux window for errors. Verify `TELEGRAM_BOT_TOKEN` and `TELEGRAM_USER_NAME` env vars are set.
- **Cannot send telegram messages**: Run `uv run telegram-send "test"` and check the error. The bot may not have received any messages yet (no chat_id to reply to).
- **Services not starting**: Check the `bootstrap` tmux window for errors. Verify `services.toml` is valid TOML.
- **Wait script behaving oddly**: Check `.runtime/wait_counter` contents. Delete it to reset: `rm -f .runtime/wait_counter`

## 3. Fix or escalate

If you can fix the issue yourself (edit a config, restart a service, etc.), do so and commit the fix.

If you cannot fix the issue, tell the user via telegram:
- What you observed
- What you tried
- What you think the problem might be
- Ask for their help

## 4. Never panic

You are a persistent agent. Even if something is broken, you will keep running. Focus on what you can do, communicate clearly with the user, and wait for help if needed.
