---
name: read-telegram-history
description: Telegram-specific implementation for reading recent conversation history. Only use when the deployment is known to use telegram (e.g. invoked from within `send-user-message` after its telegram probe succeeds). Deployments with other channels have their own equivalents.
---

# Reading telegram history

## View recent messages

```bash
uv run telegram-history --last 20
```

Each line shows the chat ID, sender, and message text:
```
[chat:12345] [@username] Hello there
[chat:12345] [you] Hi! How can I help?
```

## Filter by conversation

To see messages from a specific chat only:

```bash
uv run telegram-history --chat-id 12345 --last 20
```

## When to use

Always read recent history before replying to a telegram message so your reply makes sense in context. Use `--chat-id` to focus on the specific conversation you are replying to. You do not need to read history when proactively reaching out about something new.
