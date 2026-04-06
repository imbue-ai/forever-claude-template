"""Send a message to the user via Telegram Bot API.

Looks up the chat_id from recent history (or uses an explicit --chat-id).
Appends the outgoing message to history.

Usage:
    uv run telegram-send "Your message here"
    uv run telegram-send --chat-id 12345 "Your message here"

Environment:
    TELEGRAM_BOT_TOKEN  - Telegram Bot API token
    TELEGRAM_USER_NAME  - Username to find chat_id for (when --chat-id not given)
"""

import json
import os
import sys
from pathlib import Path
from urllib.request import Request, urlopen

from loguru import logger

HISTORY_FILE = Path("runtime/telegram/history.jsonl")


def _get_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        logger.error("{} environment variable is required", name)
        sys.exit(1)
    return value


def _find_chat_id(username: str) -> int | None:
    """Find the most recent chat_id for the given username from history."""
    if not HISTORY_FILE.exists():
        return None

    chat_id = None
    with open(HISTORY_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            message = obj.get("message")
            if not message:
                continue

            from_user = message.get("from", {})
            if from_user.get("username", "").lower() == username.lower():
                chat_id = message["chat"]["id"]

    return chat_id


def _send_message(token: str, chat_id: int, text: str) -> dict:
    """Send a message via the Telegram Bot API using POST with JSON body."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode()
    request = Request(url, data=payload, headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode())


def _append_to_history(text: str, chat_id: int) -> None:
    """Append an outgoing message to the history file."""
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "direction": "out",
        "chat_id": chat_id,
        "text": text,
    }
    with open(HISTORY_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def main() -> None:
    args = sys.argv[1:]

    # Parse --chat-id flag
    explicit_chat_id: int | None = None
    if "--chat-id" in args:
        idx = args.index("--chat-id")
        if idx + 1 >= len(args):
            logger.error("--chat-id requires a value")
            sys.exit(1)
        try:
            explicit_chat_id = int(args[idx + 1])
        except ValueError:
            logger.error("--chat-id must be an integer, got: {}", args[idx + 1])
            sys.exit(1)
        args = args[:idx] + args[idx + 2 :]

    if not args:
        logger.error("Usage: telegram-send [--chat-id ID] <message>")
        sys.exit(1)

    text = " ".join(args)
    token = _get_env("TELEGRAM_BOT_TOKEN")

    if explicit_chat_id is not None:
        chat_id = explicit_chat_id
    else:
        username = _get_env("TELEGRAM_USER_NAME")
        resolved = _find_chat_id(username)
        if resolved is None:
            logger.error(
                "No chat_id found for @{} in history. "
                "The user must send a message to the bot first, "
                "or use --chat-id to specify the chat explicitly.",
                username,
            )
            sys.exit(1)
        chat_id = resolved

    result = _send_message(token, chat_id, text)
    if not result.get("ok"):
        logger.error("sendMessage failed: {}", result)
        sys.exit(1)

    _append_to_history(text, chat_id)
    logger.info("Message sent to chat {}", chat_id)


if __name__ == "__main__":
    main()
