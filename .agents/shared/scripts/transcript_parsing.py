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


def _is_human_user_message(event: dict[str, Any]) -> bool:
    """True if a ``type: user`` event represents an actual human turn boundary.

    Excludes tool_result carriers and Claude Code's synthetic *meta* injections
    (Stop-hook feedback, Skill-tool invocation frames, etc.), which are flagged
    with ``isMeta: true``.
    """
    if event.get("type") != "user":
        return False
    if event.get("isMeta") is True:
        return False
    if is_user_tool_result_carrier(event):
        return False
    return True


def nth_user_message_index(events: list[dict[str, Any]], n: int = 0) -> int | None:
    """Index of the N-th-most-recent human user message, or ``None``.

    ``n=0`` is the most recent, ``n=1`` the one before that, and so on. Skips
    tool_result carriers and ``isMeta: true`` meta injections (Stop-hook feedback,
    Skill-tool invocation frames).
    """
    if n < 0:
        raise ValueError(f"n must be >= 0, got {n}")
    seen = 0
    for index in range(len(events) - 1, -1, -1):
        if not _is_human_user_message(events[index]):
            continue
        if seen == n:
            return index
        seen += 1
    return None


def last_user_message_index(events: list[dict[str, Any]]) -> int | None:
    """Index of the most recent human user message, or ``None`` if there is none.

    Thin wrapper around ``nth_user_message_index(events, 0)``. Kept for the
    existing detector caller. Skips tool_result carriers and ``isMeta: true``
    pseudo-messages (Stop-hook feedback, Skill-tool invocation frames) so
    callers get a true human-turn boundary rather than a synthetic injection.

    Callers slice however they like:
    - ``events[idx:]`` keeps the user message in the slice (replay use-cases).
    - ``events[idx + 1:]`` drops it (counting what happened *after* it).
    """
    return nth_user_message_index(events, 0)


def _event_text(event: dict[str, Any]) -> str:
    """Concatenated text content of an event's message, or ``""``.

    Used by marker-based slicing. Joins all ``type: text`` blocks when content
    is a list; returns content directly when it is already a string.
    """
    message = event.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(chunks)
    return ""


def find_marker_index(
    events: list[dict[str, Any]], marker: str, start: int = 0
) -> int | None:
    """Index of the first event at or after ``start`` whose text content contains ``marker``.

    Returns ``None`` if no event matches. Used as an escape hatch when counting
    turns does not line up cleanly (e.g. marker-based slicing across intervening
    sub-agent pseudo-messages).
    """
    for index in range(start, len(events)):
        if marker in _event_text(events[index]):
            return index
    return None
