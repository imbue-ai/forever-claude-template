"""Attribute tk step records to the session that owns them.

A native Claude Code Agent-tool subagent shares its parent's ``TICKETS_DIR`` and
``MNGR_AGENT_NAME``, so its ``tk`` step records land in the *same* ``.tickets/``
directory stamped with the *same* ``agent:`` name -- indistinguishable from the
main agent's steps by the ticket files alone. The only place a step's session
identity survives is the transcript:

  - ``tk start`` / ``tk close`` print ``Updated <id> -> <status>`` to tool
    output; that line appears in exactly the session that ran the transition,
    so it attributes a started/closed step *definitively*.
  - ``tk create --step "<title>"`` appears in the session that created the
    step. The created id is captured into a shell variable (``S1=$(tk create
    ...)``) so it never reaches tool output, which is why a *pending*
    (created-but-never-started) step has no transition anywhere and must be
    attributed *best-effort* by matching its title against the create commands.

This module turns those transcript signals into an ``id -> session_id`` map so
the enrichment table -- which commingles every same-name agent's steps in one
``.tickets/`` dir -- can be split into the main view's steps and each
subagent's steps. Session identity itself is derived in the backend from the
transcript file location (see ``session_watcher.py``); ``tk`` cannot stamp it
because a subagent's Bash inherits the parent process env unchanged.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Sequence
from typing import Any

from imbue.imbue_common.frozen_model import FrozenModel

# A ``tk``/``ticket`` create invocation (``super`` is the plugin-bypassing form).
_STEP_CREATE_DETECT = re.compile(r"\b(?:tk|ticket)\s+(?:super\s+)?create\b")

# The quoted title argument of a ``--step`` create. Global so a batched command
# (several ``tk create --step`` joined by ``&&`` / ``;``) yields every title,
# and tolerant of titles containing shell metacharacters such as parentheses
# because it reads only up to the matching closing quote.
_STEP_TITLE_RE = re.compile(r"--step\s+(?:\"([^\"]*)\"|'([^']*)')")

# ``Updated <id> -> <status>`` printed by tk on every transition (see
# vendor/tk/ticket). Captures the id; the status only needs to be one of the
# three valid values for the line to count as a transition.
_TRANSITION_RE = re.compile(r"Updated\s+(\S+)\s+->\s+(?:open|in_progress|closed)")


def extract_create_titles(command: str) -> list[str]:
    """Titles created by ``tk create --step "<title>"`` invocations in a full
    Bash command, in order. Returns ``[]`` when the command is not a step
    create. The caller must pass the FULL command (not the 200-char
    ``input_preview``), so a batch of creates is captured whole rather than cut
    mid-title."""
    if "--step" not in command or _STEP_CREATE_DETECT.search(command) is None:
        return []
    return [double or single for double, single in _STEP_TITLE_RE.findall(command)]


class TranscriptStepSignals(FrozenModel):
    """tk step signals extracted from one session's transcript lines."""

    # Step ids that had an ``Updated <id> -> <status>`` transition in this
    # session -- the session that ran the step's ``tk start`` / ``tk close``.
    transition_ids: tuple[str, ...]
    # Titles created via ``tk create --step`` in this session, in order (a
    # multiset: the same title may legitimately be created more than once).
    create_titles: tuple[str, ...]


def extract_step_signals(lines: Sequence[str]) -> TranscriptStepSignals:
    """Pull tk step signals out of raw Claude session JSONL lines:

      - transition ids from ``Updated <id> -> <status>`` in tool_result outputs,
      - create titles from ``tk create --step "<title>"`` in Bash tool_use inputs.

    Reads the raw JSONL so it sees the untruncated command and tool output (the
    transcript stores both in full; only the frontend view truncates them).
    """
    transition_ids: list[str] = []
    create_titles: list[str] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = raw.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "tool_use" and block.get("name") == "Bash":
                tool_input = block.get("input")
                if isinstance(tool_input, dict):
                    command = tool_input.get("command")
                    if isinstance(command, str):
                        create_titles.extend(extract_create_titles(command))
            elif block_type == "tool_result":
                text = _tool_result_text(block.get("content"))
                for match in _TRANSITION_RE.finditer(text):
                    transition_ids.append(match.group(1))
            else:
                # Other block types (assistant text, thinking) carry no step signals.
                continue
    return TranscriptStepSignals(transition_ids=tuple(transition_ids), create_titles=tuple(create_titles))


def _tool_result_text(content: Any) -> str:
    """Flatten a tool_result ``content`` (a string, or a list of text blocks)
    into plain text for transition scanning. Typed loosely because it reads
    straight off parsed JSON (see session_parser for the same convention)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
        else:
            # Non-text blocks (e.g. images) carry no transition text.
            continue
    return "\n".join(parts)


class StepAttribution(FrozenModel):
    """Transcript-derived inputs for attributing steps to sessions, aggregated
    across all of an agent's session files (main + subagents)."""

    # session_id -> ids that transitioned in that session.
    transition_ids_by_session: dict[str, tuple[str, ...]]
    # session_id -> titles created via ``tk create --step`` in that session.
    create_titles_by_session: dict[str, tuple[str, ...]]
    # The agent's main session ids -- every session id that is NOT a subagent
    # session. A step whose owner is one of these (or unknown) belongs to the
    # main view; any other owner is a subagent session.
    main_session_ids: tuple[str, ...]


def attribute_steps(
    step_records: Sequence[tuple[str, str, str]],
    attribution: StepAttribution,
) -> dict[str, str | None]:
    """Map each step ``ticket_id`` to the ``session_id`` that owns it.

    ``step_records`` is a sequence of ``(ticket_id, title, status)``. The return
    maps every ticket_id to its owning session id, or ``None`` when it cannot be
    attributed (the caller treats unknown as the main view, the safe default).

    Attribution is two-tier:

      1. *Definitive* -- a step whose id has a transition belongs to the session
         that printed it. A step transitions in exactly one session, so this is
         unambiguous.
      2. *Best-effort* -- a pending step (status ``open``, no transition) is
         matched by title to a session that has a ``tk create --step`` of that
         title with *residual capacity*: creates of that title minus the creates
         already consumed by that session's transition-attributed steps. This
         keeps a pending step from being claimed by a session whose create of
         the same title already produced a *started* step.
    """
    transition_owner: dict[str, str] = {}
    for session_id, ids in attribution.transition_ids_by_session.items():
        for ticket_id in ids:
            transition_owner[ticket_id] = session_id

    owner: dict[str, str | None] = {}
    # Per session, how many transition-attributed steps carry each title; their
    # create is "spent" and must not also be matched to a pending step.
    consumed_by_session_title: dict[str, Counter[str]] = {}
    for ticket_id, title, _status in step_records:
        session_id = transition_owner.get(ticket_id)
        if session_id is not None:
            owner[ticket_id] = session_id
            consumed_by_session_title.setdefault(session_id, Counter())[title] += 1

    # Residual create capacity per session per title.
    residual: dict[str, Counter[str]] = {}
    for session_id, titles in attribution.create_titles_by_session.items():
        capacity = Counter(titles)
        capacity.subtract(consumed_by_session_title.get(session_id, Counter()))
        residual[session_id] = capacity

    for ticket_id, title, _status in step_records:
        if ticket_id in owner:
            continue
        # Sorted for determinism. FIXME: when the same title is pending
        # simultaneously in more than one session, both have residual capacity
        # and the first sorted session wins for all of them -- so the steps may
        # land in the wrong view. Accepted as cosmetically benign: these are
        # content-less placeholders, and the misattribution self-corrects the
        # moment one of the steps starts (it then carries a definitive
        # transition).
        match: str | None = None
        for session_id in sorted(residual.keys()):
            if residual[session_id][title] > 0:
                match = session_id
                break
        if match is not None:
            residual[match][title] -= 1
            owner[ticket_id] = match
        else:
            owner[ticket_id] = None

    return owner
