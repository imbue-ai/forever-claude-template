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


@pure
def pending_tool_call(events: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the most recent assistant tool call with no matching ``tool_result``,
    as ``{tool_name, input_preview, is_codex}`` -- or ``None`` if none is in flight.

    This is the caption counterpart to :func:`has_unmatched_tool_use`: that returns
    *whether* a tool is in flight (drives the TOOL_RUNNING state); this returns
    *which* one (drives the "Running <caption>" label). ``is_codex`` is read from the
    containing assistant message's ``source`` (codex parser stamps ``codex/...``), so
    the caption layer picks the right harness's verb paths without a separate lookup.
    Walks from the end so the newest unmatched call wins.
    """
    resolved: set[str] = set()
    for event in reversed(list(events)):
        event_type = event.get("type")
        if event_type == "tool_result":
            tool_call_id = event.get("tool_call_id")
            if tool_call_id:
                resolved.add(tool_call_id)
        elif event_type == "assistant_message":
            for tool_call in reversed(list(event.get("tool_calls") or ())):
                tool_call_id = tool_call.get("tool_call_id")
                if tool_call_id and tool_call_id not in resolved:
                    source = event.get("source")
                    return {
                        "tool_name": str(tool_call.get("tool_name") or ""),
                        "input_preview": str(tool_call.get("input_preview") or ""),
                        "is_codex": isinstance(source, str) and source.startswith("codex"),
                    }
    return None


@pure
def last_event_type(events: Sequence[dict[str, Any]]) -> str | None:
    """Return the ``type`` of the final transcript event, or ``None`` if empty."""
    if not events:
        return None
    return events[-1].get("type")


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

# States in which the agent process is alive (present on this host), whether actively
# looping (RUNNING) or paused between actions (WAITING). Used to decide whether an
# in-flight tool call should read as TOOL_RUNNING: codex's async (unified) exec yields
# and the agent's lifecycle drops to WAITING while a background command runs, so a live
# tool must still show "Running" even when the agent is not RUNNING. STOPPED/DONE/REPLACED
# (dead) agents are excluded, so a stale unmatched tool from a gone process reads IDLE.
ALIVE_LIFECYCLE_STATES: frozenset[str] = RUNNING_LIFECYCLE_STATES | frozenset({"WAITING"})


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
    is_agent_alive: bool,
    has_pending_tool_use: bool,
    tail_event_at: float | None = None,
    process_started_at: float | None = None,
) -> ActivityState:
    """Derive an ``ActivityState`` from lifecycle state and transcript signals.

    Two lifecycle inputs, both from the mngr state:
    - ``is_agent_running``: the agent is actively looping (RUNNING /
      RUNNING_UNKNOWN_AGENT_TYPE) -- reasoning or generating a reply.
    - ``is_agent_alive``: the agent process exists at all (RUNNING *or* WAITING),
      i.e. not STOPPED/DONE/REPLACED.

    The distinction matters because of codex's async (unified) exec: it launches a
    command in the background, yields, and waits -- so while a long command runs the
    agent's lifecycle drops to WAITING even though a tool is genuinely in flight. A
    RUNNING-only gate would blank that to IDLE. So an in-flight tool reads as
    TOOL_RUNNING for any *alive* agent, running or not; only STOPPED/DONE agents are
    forced IDLE up front.

    ``tail_event_at`` and ``process_started_at`` feed :func:`is_transcript_tail_stale`
    (see there): they distinguish an in-flight tool from the *current* turn (fresh
    tail -> TOOL_RUNNING) from an unmatched tool left over by a turn a prior process
    abandoned mid-flight (stale tail -> IDLE), which would otherwise pin the indicator
    busy forever after a restart.

    Priority:
      0. agent not alive (STOPPED/DONE) -> IDLE.
      1. transcript tail predates the current process (stale) -> IDLE.
      2. an unmatched tool call (a tool is in flight) -> TOOL_RUNNING -- fires even
         when the agent is merely alive-and-WAITING (codex's backgrounded command).
      3. agent running, no tool in flight -> THINKING (the default busy state,
         covering reasoning / reply generation that writes nothing to the transcript
         until it completes -- the "nothing" gap).
      4. otherwise (alive but WAITING, turn settled) -> IDLE -- waiting for the user.
    """
    if not is_agent_alive:
        return ActivityState.IDLE
    if is_transcript_tail_stale(tail_event_at=tail_event_at, process_started_at=process_started_at):
        return ActivityState.IDLE
    if has_pending_tool_use:
        return ActivityState.TOOL_RUNNING
    if is_agent_running:
        return ActivityState.THINKING
    return ActivityState.IDLE
