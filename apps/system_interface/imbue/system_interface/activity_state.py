"""Per-agent activity state surfaced on the chat panel.

The state is derived from three inputs:
- the agent's mngr lifecycle state (RUNNING, STOPPED, etc.) -- a non-running
  agent is always IDLE regardless of the other signals, which prevents a
  STOPPED agent from appearing as "Thinking..." due to stale transcript data
- the parsed transcript events from the agent's session JSONL files
- the mtime of the ``claude_process_started`` marker (touched by mngr on every
  startup/resume) -- a transcript tail older than the current process is left
  over from a turn this process never ran, so it must not show "Thinking..."

We deliberately do *not* consult the legacy ``active`` marker file: it can
become stale (e.g. when Claude exits abnormally and the ``Stop`` hook never
runs to clear it), which would falsely show "Thinking..." indefinitely. The
transcript is authoritative for IDLE / THINKING / TOOL_RUNNING *within* the
current process; the ``claude_process_started`` boundary guards against a
transcript abandoned mid-turn by a prior process (e.g. a container restart),
which the transcript alone cannot distinguish from work still in flight.
"""

import re
from collections.abc import Sequence
from datetime import datetime
from enum import auto
from typing import Any

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.pure import pure


class ActivityState(UpperCaseStrEnum):
    """The activity state of a chat agent, as surfaced above the message input."""

    IDLE = auto()
    THINKING = auto()
    TOOL_RUNNING = auto()


@pure
def has_unmatched_tool_use(events: Sequence[dict[str, Any]]) -> bool:
    """True iff the transcript has at least one ``tool_use`` without a matching ``tool_result``.

    Walks every event so that an unmatched ``tool_use`` from any prior assistant
    turn still counts -- in practice Claude only ever has outstanding tool calls
    from its most recent assistant message, but the matching is order-independent
    so we don't have to care.
    """
    pending: set[str] = set()
    matched: set[str] = set()
    for event in events:
        event_type = event.get("type")
        if event_type == "assistant_message":
            for tool_call in event.get("tool_calls") or ():
                tool_call_id = tool_call.get("tool_call_id")
                if tool_call_id:
                    pending.add(tool_call_id)
        elif event_type == "tool_result":
            tool_call_id = event.get("tool_call_id")
            if tool_call_id:
                matched.add(tool_call_id)
        else:
            # user_message or other event types we don't care about for tool tracking.
            pass
    return bool(pending - matched)


# The composer's model picker / fast-mode toggle drive Claude Code by sending it
# `/model ...` and `/fast ...` slash commands (see server.py). Claude Code handles
# each locally -- it records a normalized command line plus a raw
# `<local-command-stdout>` confirmation, and NEVER produces a model reply. Neither
# line is a genuine conversational turn, so a transcript that ends on one is not
# "the user spoke and Claude is thinking". Recognized here mirroring the frontend
# detectors in message-classification.ts (matchModelFastCommand /
# matchModelFastCommandOutput) so the two stay in agreement.
_MODEL_FAST_COMMAND_RE = re.compile(r"^/(model|fast)\b")
_LOCAL_COMMAND_STDOUT_MARKER = "<local-command-stdout>"
_MODEL_FAST_STDOUT_RE = re.compile(r"Set model to|Fast mode")


@pure
def is_non_turn_tail_event(event: dict[str, Any]) -> bool:
    """True for a trailing transcript event that is NOT a genuine turn awaiting a reply.

    Two families qualify:

    - ``isMeta`` framework injections (the resume-continuation marker, the
      ``<local-command-caveat>`` wrapper, image-coordinate notes, ...): Claude
      flags these itself and never acts on them.
    - the ``/model`` / ``/fast`` slash command the composer's model picker sends,
      and its ``<local-command-stdout>`` confirmation. These are NOT flagged
      ``isMeta`` by Claude (only the caveat wrapper is), so they must be matched
      by content -- otherwise a picker change leaves the indicator stuck on
      "Thinking..." because the command line / confirmation is the transcript tail
      yet no model reply is ever coming.
    """
    if event.get("is_meta"):
        return True
    if event.get("type") != "user_message":
        return False
    content = event.get("content")
    text = content.strip() if isinstance(content, str) else ""
    if _MODEL_FAST_COMMAND_RE.match(text):
        return True
    return text.startswith(_LOCAL_COMMAND_STDOUT_MARKER) and _MODEL_FAST_STDOUT_RE.search(text) is not None


@pure
def last_event_type(events: Sequence[dict[str, Any]]) -> str | None:
    """Return the ``type`` of the last genuine-turn transcript event, or ``None``.

    Trailing non-turn events (see :func:`is_non_turn_tail_event`) are skipped so
    they cannot drive the THINKING fallback: a picker-sent ``/model`` / ``/fast``
    command and its confirmation, or an ``isMeta`` framework injection, leave the
    indicator on whatever the last real turn was rather than pinning it to
    "Thinking...". Returns ``None`` for an empty transcript or one that is entirely
    non-turn events.
    """
    for event in reversed(events):
        if is_non_turn_tail_event(event):
            continue
        return event.get("type")
    return None


@pure
def last_event_timestamp(events: Sequence[dict[str, Any]]) -> str | None:
    """Return the ISO-8601 ``timestamp`` of the final transcript event, or ``None``.

    Events are ordered by timestamp, so the final event's timestamp is the most
    recent transcript activity. Returns ``None`` for an empty transcript or a
    final event without a timestamp (e.g. an injected non-transcript event).
    """
    if not events:
        return None
    timestamp = events[-1].get("timestamp")
    return timestamp if isinstance(timestamp, str) and timestamp else None


@pure
def parse_iso_timestamp_to_epoch(timestamp: str | None) -> float | None:
    """Parse an ISO-8601 transcript timestamp (e.g. ``2026-06-08T19:42:15.191Z``)
    into epoch seconds, or ``None`` if it is missing or unparseable.

    Claude writes UTC timestamps with a trailing ``Z``; ``fromisoformat`` accepts
    that on the Python versions this app targets. The result is an absolute epoch,
    directly comparable to a filesystem mtime regardless of timezone.
    """
    if not timestamp:
        return None
    try:
        return datetime.fromisoformat(timestamp).timestamp()
    except ValueError:
        return None


RUNNING_LIFECYCLE_STATES: frozenset[str] = frozenset({"RUNNING", "RUNNING_UNKNOWN_AGENT_TYPE"})


@pure
def is_transcript_tail_stale(
    *,
    tail_event_at: float | None,
    process_started_at: float | None,
) -> bool:
    """True iff the newest transcript event predates the current Claude process.

    ``tail_event_at`` is the epoch time of the final transcript event;
    ``process_started_at`` is the mtime of the agent's ``claude_process_started``
    marker, which mngr touches on every startup/resume (a fresh, not-mid-turn
    process). When the newest event is older than that boundary, it belongs to a
    turn the *current* process never ran -- e.g. a turn abandoned mid-flight when
    a container restart killed Claude. Its "still working" tail (an unmatched
    ``tool_use`` or a trailing ``tool_result``) would otherwise pin the indicator
    at TOOL_RUNNING / THINKING forever, since the dead turn will never emit the
    closing ``assistant_message`` that settles it back to IDLE.

    Returns ``False`` when either input is missing (no marker yet, or a final
    event without a timestamp): we only override on positive evidence of
    staleness, otherwise the transcript signals stand.
    """
    if tail_event_at is None or process_started_at is None:
        return False
    return tail_event_at < process_started_at


@pure
def derive_activity_state(
    *,
    is_agent_running: bool,
    has_pending_tool_use: bool,
    tail_event_type: str | None,
    tail_event_at: float | None = None,
    process_started_at: float | None = None,
) -> ActivityState:
    """Derive an ``ActivityState`` from lifecycle state and transcript signals.

    ``is_agent_running`` reflects the mngr lifecycle state: ``True`` when the
    agent is in a running state (RUNNING, RUNNING_UNKNOWN_AGENT_TYPE), ``False``
    otherwise (STOPPED, WAITING, REPLACED, DONE, etc.). A non-running agent is
    always IDLE regardless of transcript contents, which prevents a STOPPED agent
    from appearing as "Thinking..." due to stale transcript data.

    ``tail_event_type`` is the cached result of :func:`last_event_type` for the
    agent's current transcript (named distinctly from the helper to avoid
    shadowing it in this scope). ``tail_event_at`` and ``process_started_at`` feed
    :func:`is_transcript_tail_stale` (see there): together they detect a tail left
    over from before the current process started, which a running-but-idle agent
    would otherwise show as "Thinking..." indefinitely after a mid-turn restart.

    Priority:
      0. agent not running -> IDLE.
      1. transcript tail predates the current process (stale) -> IDLE.
      2. unmatched ``tool_use`` -> TOOL_RUNNING.
      3. last genuine-turn event is ``user_message`` or ``tool_result`` -> THINKING
         (Claude has been handed input but hasn't replied yet). ``last_event_type``
         has already skipped trailing non-turn events -- an ``isMeta`` injection or
         the picker's ``/model`` / ``/fast`` command + confirmation -- so those do
         not count as "handed input".
      4. otherwise (last event is ``assistant_message`` or transcript is empty)
         -> IDLE.
    """
    if not is_agent_running:
        return ActivityState.IDLE
    if is_transcript_tail_stale(tail_event_at=tail_event_at, process_started_at=process_started_at):
        return ActivityState.IDLE
    if has_pending_tool_use:
        return ActivityState.TOOL_RUNNING
    if tail_event_type in ("user_message", "tool_result"):
        return ActivityState.THINKING
    return ActivityState.IDLE
