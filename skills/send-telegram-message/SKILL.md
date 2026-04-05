---
name: send-telegram-message
description: Send a message to the user via Telegram. Use this whenever you need to communicate with the user.
---

# Sending a message to the user

Run this command to send a message:

```bash
uv run telegram-send "Your message here"
```

The command looks up the user's chat ID from the conversation history and sends the message via the Telegram Bot API.

## Guidelines

- Keep messages concise and actionable.
- When asking questions, provide numbered options to make it easy for the user to reply quickly.
- Always include a final option that encourages the user to type their own response.
- Before replying to a message, use the `read-telegram-history` skill to understand the conversation context.
- When notifying about completed work, include a summary of what was done.
