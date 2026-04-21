#!/usr/bin/env python3
"""Extract the events from the most recent turn of a Claude Code session.

Given a path to a session transcript JSONL (as exported to
``$CLAUDE_TRANSCRIPT_PATH`` by the Stop hook), copy everything after the most
recent human user message into a destination JSONL file. Used by the
``crystallize-task`` skill to hand the worker a focused replay of just the
turn it needs to crystallize.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _load_events(transcript: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with transcript.open(encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                events.append(parsed)
    return events


def _is_user_tool_result_carrier(event: dict[str, Any]) -> bool:
    message = event.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if not isinstance(content, list) or not content:
        return False
    return all(
        isinstance(block, dict) and block.get("type") == "tool_result" for block in content
    )


def _last_turn_slice(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    split_index = 0
    for index in range(len(events) - 1, -1, -1):
        event = events[index]
        if event.get("type") != "user":
            continue
        if _is_user_tool_result_carrier(event):
            continue
        split_index = index
        break
    return events[split_index:]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--transcript",
        required=True,
        type=Path,
        help="Path to the Claude Code session JSONL transcript.",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Path where the extracted turn JSONL will be written.",
    )
    args = parser.parse_args()

    if not args.transcript.is_file():
        print(f"transcript not found: {args.transcript}", file=sys.stderr)
        return 1

    events = _load_events(args.transcript)
    turn_events = _last_turn_slice(events)
    if not turn_events:
        print("no turn found (transcript empty or malformed)", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for event in turn_events:
            handle.write(json.dumps(event) + "\n")
    print(f"wrote {len(turn_events)} events to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
