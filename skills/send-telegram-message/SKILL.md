---
name: send-telegram-message
description: Send a message to the user via Telegram. Use this whenever you need to communicate with the user.
---

# Sending a message to the user

## Choosing the right conversation

Incoming messages include a chat ID (shown in the `mngr message` payload and in `telegram-history` output). Always reply to the same chat the user messaged from.

Use `uv run telegram-history --last 10` to see recent messages with their chat IDs, then pass the correct chat ID when sending.

## Sending a message

Reply to a specific conversation:

```bash
uv run telegram-send --chat-id <CHAT_ID> "Your message here"
```

If you omit `--chat-id`, the command falls back to the most recent chat from the configured user. This is fine for simple single-chat setups, but always prefer specifying the chat ID when replying to a message.

```bash
uv run telegram-send "Your message here"
```

## Guidelines

- Always reply in the same chat the user messaged from.
- Keep messages concise and actionable.
- When asking questions, provide numbered options to make it easy for the user to reply quickly.
- Always include a final option that encourages the user to type their own response.
- Before replying to a message, use the `read-telegram-history` skill to understand the conversation context.
- When notifying about completed work, include a summary of what was done.
