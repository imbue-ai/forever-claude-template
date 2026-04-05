"""Read and display telegram conversation history.

Usage: uv run telegram-history [--last N]
"""

import json
import sys
from pathlib import Path


HISTORY_FILE = Path(".runtime/telegram/history.jsonl")


def _format_entry(obj: dict) -> str | None:
    """Format a history entry as a human-readable line."""
    if "direction" in obj and obj["direction"] == "out":
        return f"[you] {obj.get('text', '')}"

    message = obj.get("message")
    if message:
        user = message.get("from", {}).get("username", "unknown")
        text = message.get("text", "")
        if text:
            return f"[@{user}] {text}"

    return None


def main() -> None:
    last_n = 20
    args = sys.argv[1:]
    if "--last" in args:
        idx = args.index("--last")
        if idx + 1 < len(args):
            try:
                last_n = int(args[idx + 1])
            except ValueError:
                print("Error: --last requires a number", file=sys.stderr)
                sys.exit(1)

    if not HISTORY_FILE.exists():
        print("No history found.")
        return

    lines = HISTORY_FILE.read_text().strip().split("\n")
    lines = lines[-last_n:] if last_n else lines

    for line in lines:
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        formatted = _format_entry(obj)
        if formatted:
            print(formatted)


if __name__ == "__main__":
    main()
