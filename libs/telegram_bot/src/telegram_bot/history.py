"""Read and display telegram conversation history.

Usage:
    uv run telegram-history [--last N] [--chat-id ID]
"""

import json
import sys
from pathlib import Path

HISTORY_FILE = Path("runtime/telegram/history.jsonl")


def _get_chat_id(obj: dict) -> int | None:
    """Extract chat_id from a history entry."""
    if "chat_id" in obj:
        return obj["chat_id"]
    message = obj.get("message")
    if message:
        return message.get("chat", {}).get("id")
    return None


def _format_entry(obj: dict) -> str | None:
    """Format a history entry as a human-readable line including chat_id."""
    chat_id = _get_chat_id(obj)
    chat_prefix = f"[chat:{chat_id}]" if chat_id else "[chat:?]"

    if "direction" in obj and obj["direction"] == "out":
        return f"{chat_prefix} [you] {obj.get('text', '')}"

    message = obj.get("message")
    if message:
        user = message.get("from", {}).get("username", "unknown")
        text = message.get("text", "")
        if text:
            return f"{chat_prefix} [@{user}] {text}"

    return None


def main() -> None:
    last_n = 20
    filter_chat_id: int | None = None
    args = sys.argv[1:]

    if "--last" in args:
        idx = args.index("--last")
        if idx + 1 < len(args):
            try:
                last_n = int(args[idx + 1])
            except ValueError:
                print("Error: --last requires a number", file=sys.stderr)
                sys.exit(1)

    if "--chat-id" in args:
        idx = args.index("--chat-id")
        if idx + 1 < len(args):
            try:
                filter_chat_id = int(args[idx + 1])
            except ValueError:
                print("Error: --chat-id requires a number", file=sys.stderr)
                sys.exit(1)

    if not HISTORY_FILE.exists():
        print("No history found.")
        return

    lines = HISTORY_FILE.read_text().strip().split("\n")

    entries: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        if filter_chat_id is not None and _get_chat_id(obj) != filter_chat_id:
            continue

        formatted = _format_entry(obj)
        if formatted:
            entries.append(formatted)

    for entry in entries[-last_n:]:
        print(entry)


if __name__ == "__main__":
    main()
