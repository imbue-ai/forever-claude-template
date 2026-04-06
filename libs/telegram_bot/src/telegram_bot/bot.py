"""Telegram bot that long-polls for messages and delivers them via mngr message.

Filters messages by TELEGRAM_USER_NAME. Appends raw update JSON to
runtime/telegram/history.jsonl. Calls mngr message for each new text message.

Environment:
    TELEGRAM_BOT_TOKEN  - Telegram Bot API token
    TELEGRAM_USER_NAME  - Username to accept messages from (case-insensitive)
    MNGR_AGENT_NAME     - Agent name to send messages to via mngr message
"""

import json
import os
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from loguru import logger

HISTORY_FILE = Path("runtime/telegram/history.jsonl")
POLL_TIMEOUT = 30  # seconds (Telegram long polling)
ERROR_BACKOFF_SECONDS = 5


def _get_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        logger.error("{} environment variable is required", name)
        sys.exit(1)
    return value


def _telegram_api(token: str, method: str, params: dict | None = None) -> dict:
    """Call the Telegram Bot API."""
    url = f"https://api.telegram.org/bot{token}/{method}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = Request(url)
    with urlopen(request, timeout=POLL_TIMEOUT + 10) as response:
        return json.loads(response.read().decode())


def _append_to_history(update: dict) -> None:
    """Append a raw update JSON object to the history file."""
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_FILE, "a") as f:
        f.write(json.dumps(update) + "\n")


def _send_to_agent(agent_name: str, username: str, text: str, chat_id: int) -> None:
    """Send a telegram message to the agent via mngr message."""
    message = f"telegram message from @{username} (chat_id={chat_id}): {text}"
    try:
        subprocess.run(
            ["mngr", "message", agent_name, "-m", message],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        logger.warning("mngr message failed: {}", e.stderr)


def main() -> None:
    token = _get_env("TELEGRAM_BOT_TOKEN")
    expected_username = _get_env("TELEGRAM_USER_NAME").lower()
    agent_name = _get_env("MNGR_AGENT_NAME")

    offset = 0

    logger.info("Telegram bot started. Accepting messages from @{}", expected_username)
    logger.info("Delivering to agent: {}", agent_name)

    while True:
        try:
            params = {
                "timeout": str(POLL_TIMEOUT),
                "allowed_updates": json.dumps(["message"]),
            }
            if offset:
                params["offset"] = str(offset)

            result = _telegram_api(token, "getUpdates", params)

            if not result.get("ok"):
                logger.warning("getUpdates returned not ok: {}", result)
                time.sleep(ERROR_BACKOFF_SECONDS)
                continue

            updates = result.get("result", [])

            for update in updates:
                offset = update["update_id"] + 1
                _append_to_history(update)

                message = update.get("message")
                if not message:
                    continue

                from_user = message.get("from", {})
                username = from_user.get("username", "")

                if username.lower() != expected_username:
                    continue

                text = message.get("text")
                if not text:
                    continue

                chat_id = message["chat"]["id"]
                logger.info(
                    "Message from @{} in chat {}: {}...", username, chat_id, text[:100]
                )
                _send_to_agent(agent_name, username, text, chat_id)

        except (HTTPError, URLError, TimeoutError) as e:
            logger.warning("Network error: {}", e)
            time.sleep(ERROR_BACKOFF_SECONDS)
        except json.JSONDecodeError as e:
            logger.warning("JSON decode error: {}", e)
            time.sleep(ERROR_BACKOFF_SECONDS)


if __name__ == "__main__":
    main()
