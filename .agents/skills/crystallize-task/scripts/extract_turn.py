#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Extract the events from a turn of a Claude Code session.

Given a path to a session transcript JSONL (or discovered automatically from the
runtime environment), copy a turn-shaped slice into a destination JSONL file.
Used by the ``crystallize-task`` / ``heal-skill`` / ``update-skill`` skills to
hand the worker a focused replay of just the turn it needs.

Slice selection (mutually exclusive):
- ``--nth N`` (default ``0``) selects the N-th-most-recent human user message as
  the slice start. ``N == 0`` slices to end-of-transcript; ``N > 0`` ends the
  slice at the (N-1)-th-most-recent human user message (exclusive), so the slice
  covers exactly the chosen turn.
- ``--start-marker TEXT`` [+ ``--end-marker TEXT``] slices from the first event
  whose text contains ``--start-marker`` to the first subsequent event containing
  ``--end-marker`` (exclusive). Escape hatch when counting turns does not line up
  cleanly (e.g. turns interleaved with sub-agent pseudo-messages).

Transcript path resolution (in order):
1. ``--transcript`` command-line flag, if provided.
2. ``$CLAUDE_TRANSCRIPT_PATH`` env var (set inside Claude Code hook scripts).
3. ``$MNGR_CLAUDE_SESSION_ID`` combined with ``$CLAUDE_CONFIG_DIR/projects/``
   -- find a ``<session-id>.jsonl`` file anywhere under the projects tree.
   This is concurrent-session-safe because every session has a unique id.
4. ``$MNGR_AGENT_STATE_DIR/claude_session_id`` (a file written by mngr) --
   read the session id from disk and resolve as in (3). This makes the
   script work in a standard mngr agent where neither env var from (2) nor
   (3) is exported into the Bash tool's environment.

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

from transcript_parsing import (
    find_marker_index,
    iter_transcript,
    nth_user_message_index,
)


def _nth_turn_slice(events: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    """Slice covering the N-th-most-recent human turn (0 = current).

    For ``n == 0`` the slice runs from the most recent human user message to
    end-of-transcript. For ``n > 0`` it runs from the N-th-most-recent human
    user message to the (N-1)-th-most-recent (exclusive), bounding the chosen
    turn on both sides.
    """
    start = nth_user_message_index(events, n)
    if start is None:
        return [] if n > 0 else events
    if n == 0:
        return events[start:]
    # If nth_user_message_index(events, n) succeeded, nth_user_message_index
    # (events, n-1) must also succeed, so no None-check is needed here.
    end = nth_user_message_index(events, n - 1)
    assert end is not None
    return events[start:end]


def _marker_slice(
    events: list[dict[str, Any]],
    start_marker: str,
    end_marker: str | None,
) -> list[dict[str, Any]]:
    start = find_marker_index(events, start_marker)
    if start is None:
        return []
    if end_marker is None:
        return events[start:]
    end = find_marker_index(events, end_marker, start + 1)
    if end is None:
        print(
            f"warning: --end-marker {end_marker!r} not found after start; "
            "slice runs to end-of-transcript",
            file=sys.stderr,
        )
        return events[start:]
    return events[start:end]


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
        # Fallback: read the session id from $MNGR_AGENT_STATE_DIR/claude_session_id,
        # which is present inside a standard mngr agent even when
        # MNGR_CLAUDE_SESSION_ID is not exported into the shell environment.
        state_dir = environ.get("MNGR_AGENT_STATE_DIR")
        if state_dir:
            session_file = Path(state_dir) / "claude_session_id"
            if session_file.is_file():
                session_id = session_file.read_text(encoding="utf-8").strip() or None
    if not session_id:
        raise FileNotFoundError(
            "No --transcript flag, $CLAUDE_TRANSCRIPT_PATH, "
            "$MNGR_CLAUDE_SESSION_ID, or $MNGR_AGENT_STATE_DIR/claude_session_id "
            "available; cannot auto-discover transcript."
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
            "If omitted, the transcript is resolved from $CLAUDE_TRANSCRIPT_PATH, "
            "$MNGR_CLAUDE_SESSION_ID, or $MNGR_AGENT_STATE_DIR/claude_session_id."
        ),
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Path where the extracted turn JSONL will be written.",
    )
    parser.add_argument(
        "--nth",
        type=int,
        default=0,
        help=(
            "Which human turn to extract, counted back from the end. "
            "0 (default) = most recent; 1 = previous; etc. N > 0 bounds the "
            "slice on both sides (start inclusive at the Nth-most-recent human "
            "user message, end exclusive at the (N-1)th). "
            "Mutually exclusive with --start-marker / --end-marker."
        ),
    )
    parser.add_argument(
        "--start-marker",
        type=str,
        default=None,
        help=(
            "Text to match as slice start. The first event whose text content "
            "contains this string begins the slice. Escape hatch for when --nth "
            "does not line up cleanly (e.g. intervening sub-agent pseudo-messages)."
        ),
    )
    parser.add_argument(
        "--end-marker",
        type=str,
        default=None,
        help=(
            "Text to match as slice end (exclusive). Only valid with "
            "--start-marker. If omitted, the slice runs to end-of-transcript."
        ),
    )
    args = parser.parse_args()

    if args.start_marker is not None and args.nth != 0:
        parser.error("--nth is mutually exclusive with --start-marker")
    if args.end_marker is not None and args.start_marker is None:
        parser.error("--end-marker requires --start-marker")
    if args.nth < 0:
        parser.error("--nth must be >= 0")

    try:
        transcript = resolve_transcript_path(args.transcript)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if not transcript.is_file():
        print(f"transcript not found: {transcript}", file=sys.stderr)
        return 1

    events = iter_transcript(transcript)
    if args.start_marker is not None:
        turn_events = _marker_slice(events, args.start_marker, args.end_marker)
    else:
        turn_events = _nth_turn_slice(events, args.nth)
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
