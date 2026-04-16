---
name: read-telegram-history
description: Read recent Telegram conversation history. Use this to understand context before replying to a message.
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
