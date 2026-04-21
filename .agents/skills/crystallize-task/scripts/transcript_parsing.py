# /// script
# requires-python = ">=3.11"
# ///
"""Shared helpers for parsing Claude Code session JSONL transcripts.

Used by both the Stop-hook detector (``scripts/detect_crystallization_candidate.py``)
and the worker turn-extractor (``extract_turn.py``). Kept dependency-free so it
can be imported by either a top-level hook script or a PEP 723 worker script
without dragging in a virtualenv.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def iter_transcript(transcript_path: Path) -> list[dict[str, Any]]:
    """Return transcript events as a list; tolerates malformed lines."""
    events: list[dict[str, Any]] = []
    with transcript_path.open(encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                events.append(parsed)
    return events


def is_user_tool_result_carrier(event: dict[str, Any]) -> bool:
    """True if a ``type: user`` event is just wrapping tool_result blocks.

    Claude Code emits these synthetic user events to deliver tool results back
    to the model; they are not human messages, so callers that want the *human*
    turn boundary skip them.
    """
    message = event.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if not isinstance(content, list) or not content:
        return False
    return all(
        isinstance(block, dict) and block.get("type") == "tool_result" for block in content
    )


def last_user_message_index(events: list[dict[str, Any]]) -> int | None:
    """Index of the most recent human user message, or ``None`` if there is none.

    Callers slice however they like:
    - ``events[idx:]`` keeps the user message in the slice (replay use-cases).
    - ``events[idx + 1:]`` drops it (counting what happened *after* it).
    """
    for index in range(len(events) - 1, -1, -1):
        event = events[index]
        if event.get("type") != "user":
            continue
        if is_user_tool_result_carrier(event):
            continue
        return index
    return None
