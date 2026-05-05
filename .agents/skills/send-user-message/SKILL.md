---
name: send-user-message
description: Send a message to the user through whatever channel the deployment has configured. Use whenever another skill tells you to "ask the user" or "tell the user" something. Dispatches to a specific channel (e.g. telegram) if available, otherwise falls back to inline output in the current chat.
---

# Sending a message to the user

This skill is the generic entry point for agent-to-user communication. Other
skills should refer to this skill rather than naming a specific channel --
that way the channel implementation can change without touching every skill.

## Channel selection

Pick the first channel whose probe passes, in this order:

### 1. Telegram

Probe:

```bash
[ -n "${TELEGRAM_BOT_TOKEN:-}" ] && tmux has-session -t telegram 2>/dev/null
```

If both conditions hold, use the `send-telegram-message` skill for the
actual send. (Before you send, `read-telegram-history` to get the
conversation context and correct chat ID.)

### 2. Inline fallback

If no channel probe passes, just write the message as plain text in your
current response. That is the user's primary chat for this deployment; the
agent's reply IS how the user sees the question.

In the inline case, a good format is:

> @user: <your message>

so it reads like a direct message rather than narration.

## Guidelines (all channels)

- Keep messages concise and actionable.
- When asking questions, provide numbered options to make it easy to reply
  quickly, plus a final "or type your own response" option.
- When notifying about completed work, include a short summary of what was
  done.
- Never invent fake URLs or fabricate details.

## For skill authors

When writing a SKILL.md that needs to talk to the user, refer to this
skill (`send-user-message`) rather than naming a specific channel. If the
deployment changes channels later (slack, email, etc.), routing is updated
here and no caller skills need to change.
