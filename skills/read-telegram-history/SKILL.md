---
name: read-telegram-history
description: Read recent Telegram conversation history. Use this to understand context before replying to a message.
---

# Reading telegram history

The conversation history is stored in `.runtime/telegram/history.jsonl`. Each line is a raw Telegram update JSON object for incoming messages, or a JSON object with `"direction": "out"` for outgoing messages.

## Quick commands

View the last 20 messages:

```bash
uv run telegram-history --last 20
```

Or read the raw JSONL directly:

```bash
tail -n 20 .runtime/telegram/history.jsonl
```

Extract just the text of recent messages:

```bash
tail -n 20 .runtime/telegram/history.jsonl | python3 -c "
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    obj = json.loads(line)
    if 'direction' in obj and obj['direction'] == 'out':
        print(f'[you] {obj.get(\"text\", \"\")}')
    elif 'message' in obj:
        msg = obj['message']
        user = msg.get('from', {}).get('username', 'unknown')
        print(f'[@{user}] {msg.get(\"text\", \"\")}')
"
```

## When to use

Always read recent history before replying to a telegram message, so your reply makes sense in context. You do not need to read history when proactively reaching out about something new.
