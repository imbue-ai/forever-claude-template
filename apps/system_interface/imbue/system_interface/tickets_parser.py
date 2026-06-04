"""Parse tk ticket markdown files into task state.

Used by the AgentTicketsWatcher to convert `.tickets/<id>.md` files into
common task events that flow through the same pipeline as session events
(see session_parser.py for the analogous session-side flow).

A tk ticket file looks like:

    ---
    id: tt-2efd
    status: closed
    deps: []
    links: []
    created: 2026-04-28T01:17:08Z
    type: task
    priority: 2
    assignee: ...
    ---
    # Look through your recent changes to find the new theme

    Optional description body...

    ## Summary

    Found a new "midnight" theme in your settings file.

`tk close <id> "summary"` writes the one-line close summary into its own
`## Summary` section. That text is the ticket's "summary" -- rendered under
the task in the chat progress view when the ticket is closed. No timestamp is
involved: the section is written verbatim and read verbatim.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger as _loguru_logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel

logger = _loguru_logger

_VALID_STATUSES = frozenset({"open", "in_progress", "closed"})

# The heading that opens the close-summary section written by `tk close`.
_SUMMARY_HEADING = "## Summary"


class TicketState(FrozenModel):
    """Parsed snapshot of a tk ticket file at a single point in time."""

    ticket_id: str = Field(description="Ticket id from frontmatter")
    title: str = Field(description="H1 title from the body")
    status: str = Field(description="open | in_progress | closed")
    created_at: str = Field(description="frontmatter `created` field, ISO-8601")
    # `tk start` / `tk close` stamp these into the frontmatter so the watcher
    # can timestamp the in_progress / closed transitions from the ticket file
    # itself (the source of truth) rather than inferring them from file mtime.
    # Empty string when absent -- either the status hasn't been reached, or
    # the ticket was written by an older tk that didn't stamp them (the
    # watcher then falls back to mtime).
    started_at: str = Field(description="frontmatter `started` field, ISO-8601, or empty string")
    closed_at: str = Field(description="frontmatter `closed` field, ISO-8601, or empty string")
    summary: str | None = Field(description="Close summary from the `## Summary` section, or None")
    # The mngr agent that created the ticket, captured from $MNGR_AGENT_NAME
    # by the patched `tk create` (see vendor/tk/ticket). Empty string when
    # the ticket was created outside an mngr context or by an older tk that
    # did not stamp the field; the watcher uses this together with `step`
    # and `assignee` to decide whether to surface a ticket to a given
    # agent's progress view.
    agent: str = Field(description="frontmatter `agent` field, or empty string")
    # True when the ticket is a turn-bound progress record ("step"), False
    # for regular tickets. `tk create --step` stamps `step: true` into the
    # frontmatter; absent/empty parses as False (legacy + non-step
    # tickets). Drives the watcher's per-agent surfacing rule: steps go
    # only to their creator, regular tickets go to their assignee.
    step: bool = Field(description="True when this ticket is a turn-bound progress record")
    # The id of the parent ticket, if any. Used by the chat progress view
    # to nest step children under the regular ticket their agent picked
    # up. Empty string when there is no parent.
    parent_id: str = Field(description="frontmatter `parent` field, or empty string")
    # The agent (or human) currently assigned to the ticket. For regular
    # tickets this is the load-bearing "this is now my work" signal --
    # the watcher surfaces a ticket to whichever agent is the assignee
    # (so picked-up tickets show in the picker's chat, not the
    # originator's). Empty for unassigned tickets.
    assignee: str = Field(description="frontmatter `assignee` field, or empty string")


def parse_ticket_text(text: str) -> TicketState | None:
    """Parse a tk ticket markdown body. Returns None if the file isn't a
    valid tk ticket (no frontmatter, missing required fields, etc)."""
    if not text.startswith("---\n"):
        return None
    front_end = text.find("\n---\n", 4)
    if front_end < 0:
        return None
    frontmatter = text[4:front_end]
    body = text[front_end + len("\n---\n") :]

    fields: dict[str, str] = {}
    for line in frontmatter.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        fields[key.strip()] = value.strip()

    ticket_id = fields.get("id", "")
    status = fields.get("status", "")
    created_at = fields.get("created", "")
    started_at = fields.get("started", "")
    closed_at = fields.get("closed", "")
    agent = fields.get("agent", "")
    step = fields.get("step", "").lower() == "true"
    parent_id = fields.get("parent", "")
    assignee = fields.get("assignee", "")

    if not ticket_id or status not in _VALID_STATUSES:
        return None

    # Title is the first H1 in the body. Falls back to ticket_id if absent.
    title = ticket_id
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            title = stripped[2:].strip() or ticket_id
            break

    summary = _extract_summary_section(body)

    return TicketState(
        ticket_id=ticket_id,
        title=title,
        status=status,
        created_at=created_at,
        started_at=started_at,
        closed_at=closed_at,
        summary=summary,
        agent=agent,
        step=step,
        parent_id=parent_id,
        assignee=assignee,
    )


def parse_ticket_file(path: Path) -> TicketState | None:
    """Read a ticket file from disk and parse it. Returns None on read
    failure or invalid content."""
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError) as e:
        logger.debug("Skipping unreadable ticket file {}: {}", path, e)
        return None
    return parse_ticket_text(text)


def _extract_summary_section(body: str) -> str | None:
    """Return the text of the `## Summary` section, or None if absent.

    `tk close <id> "summary"` writes the one-line close summary into its own
    `## Summary` section (see vendor/tk/ticket). The section runs from the
    heading line to the next `## ` heading or EOF; its stripped contents are
    the summary. No timestamp is parsed -- the text is read verbatim, so it is
    robust to any character the summary may contain.

    The `## Summary` marker must appear as a heading -- a line whose stripped
    contents are exactly `## Summary`. A bare substring match would be a
    false-positive risk if the ticket description prose happens to contain
    `## Summary` mid-line.
    """
    lines = body.splitlines()
    start: int | None = None
    for i, line in enumerate(lines):
        if line.strip() == _SUMMARY_HEADING:
            start = i + 1
            break
    if start is None:
        return None

    collected: list[str] = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        collected.append(line)
    text = "\n".join(collected).strip()
    return text or None
