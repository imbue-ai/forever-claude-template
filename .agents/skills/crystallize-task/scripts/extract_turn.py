#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Extract the events from the most recent turn of a Claude Code session.

Given a path to a session transcript JSONL (or discovered automatically from the
runtime environment), copy everything after the most recent human user message
into a destination JSONL file. Used by the ``crystallize-task`` / ``heal-skill``
/ ``update-skill`` skills to hand the worker a focused replay of just the turn
it needs.

Transcript path resolution (in order):
1. ``--transcript`` command-line flag, if provided.
2. ``$CLAUDE_TRANSCRIPT_PATH`` env var (set inside Claude Code hook scripts).
3. ``$MNGR_CLAUDE_SESSION_ID`` combined with ``$CLAUDE_CONFIG_DIR/projects/``
   -- find a ``<session-id>.jsonl`` file anywhere under the projects tree.
   This is concurrent-session-safe because every session has a unique id.

We deliberately do NOT fall back to an mtime scan: the most-recently-modified
transcript may belong to a sibling session writing concurrently, not to the
caller's session.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from transcript_parsing import iter_transcript, last_user_message_index


def _last_turn_slice(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    boundary = last_user_message_index(events)
    if boundary is None:
        return events
    return events[boundary:]


def resolve_transcript_path(
    explicit: Path | None,
    env: dict[str, str] | None = None,
) -> Path:
    """Resolve the transcript path using explicit arg, env vars, or session-id search.

    Raises ``FileNotFoundError`` if none of the resolution strategies succeed.
    """
    environ = env if env is not None else dict(os.environ)

    if explicit is not None:
        return explicit

    hook_path = environ.get("CLAUDE_TRANSCRIPT_PATH")
    if hook_path:
        return Path(hook_path)

    session_id = environ.get("MNGR_CLAUDE_SESSION_ID")
    if not session_id:
        raise FileNotFoundError(
            "No --transcript flag, $CLAUDE_TRANSCRIPT_PATH, or "
            "$MNGR_CLAUDE_SESSION_ID available; cannot auto-discover transcript."
        )

    projects_root = Path(environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))) / "projects"
    if not projects_root.is_dir():
        raise FileNotFoundError(
            f"Projects directory not found at {projects_root}; cannot find "
            f"transcript for session {session_id}."
        )

    # Session IDs are unique, so finding by basename is safe across concurrent
    # sessions. Pick the first hit (there should be only one in practice).
    matches = sorted(projects_root.rglob(f"{session_id}.jsonl"))
    # Exclude transcripts inside subagents/ subdirectories -- those are
    # sub-sessions, not the primary session the caller is running inside.
    primary_matches = [m for m in matches if "subagents" not in m.parts]
    if primary_matches:
        return primary_matches[0]
    if matches:
        return matches[0]

    raise FileNotFoundError(
        f"No transcript matching {session_id}.jsonl under {projects_root}."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--transcript",
        type=Path,
        default=None,
        help=(
            "Path to the Claude Code session JSONL transcript. "
            "If omitted, the transcript is resolved from $CLAUDE_TRANSCRIPT_PATH "
            "or $MNGR_CLAUDE_SESSION_ID."
        ),
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Path where the extracted turn JSONL will be written.",
    )
    args = parser.parse_args()

    try:
        transcript = resolve_transcript_path(args.transcript)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if not transcript.is_file():
        print(f"transcript not found: {transcript}", file=sys.stderr)
        return 1

    events = iter_transcript(transcript)
    turn_events = _last_turn_slice(events)
    if not turn_events:
        print("no turn found (transcript empty or malformed)", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for event in turn_events:
            handle.write(json.dumps(event) + "\n")
    print(f"wrote {len(turn_events)} events to {args.output} (from {transcript})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
