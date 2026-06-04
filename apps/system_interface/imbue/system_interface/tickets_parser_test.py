"""Unit tests for the tk ticket parser."""

from __future__ import annotations

from imbue.system_interface.tickets_parser import parse_ticket_text


def test_parse_open_ticket_with_no_summary() -> None:
    """A freshly-created tk ticket has status open and no Summary section."""
    text = """---
id: tt-2efd
status: open
deps: []
links: []
created: 2026-04-28T01:17:08Z
type: task
priority: 2
assignee: Test User
---
# Look through your recent changes to find the new theme
"""
    result = parse_ticket_text(text)
    assert result is not None
    assert result.ticket_id == "tt-2efd"
    assert result.title == "Look through your recent changes to find the new theme"
    assert result.status == "open"
    assert result.created_at == "2026-04-28T01:17:08Z"
    assert result.summary is None
    # An open ticket has not started or closed -- those fields are absent.
    assert result.started_at == ""
    assert result.closed_at == ""


def test_parse_started_and_closed_timestamps() -> None:
    """`tk start` / `tk close` stamp `started:` / `closed:` into the
    frontmatter; the parser surfaces them so the watcher can timestamp the
    in_progress / closed transitions from the file itself."""
    text = """---
id: tt-2efd
status: closed
deps: []
links: []
created: 2026-04-28T01:17:08.000000Z
started: 2026-04-28T01:18:30.250000Z
closed: 2026-04-28T01:25:00.750000Z
type: task
priority: 2
---
# Register the new theme and update the toggle
"""
    result = parse_ticket_text(text)
    assert result is not None
    assert result.started_at == "2026-04-28T01:18:30.250000Z"
    assert result.closed_at == "2026-04-28T01:25:00.750000Z"


def test_parse_in_progress_ticket() -> None:
    text = """---
id: tt-2efd
status: in_progress
deps: []
links: []
created: 2026-04-28T01:17:08Z
type: task
priority: 2
---
# Trace how the dark mode toggle picks a theme
"""
    result = parse_ticket_text(text)
    assert result is not None
    assert result.status == "in_progress"
    assert result.summary is None


def test_parse_closed_ticket_with_summary_section() -> None:
    """The body of the `## Summary` section becomes the summary. This fixture
    mirrors exactly what `tk close <id> "summary"` writes -- microsecond
    `closed:` timestamp and the summary in its own untimestamped section -- so
    the parser is exercised against tk's real output, not a hand-tuned form."""
    text = """---
id: tt-2efd
status: closed
deps: []
links: []
created: 2026-04-28T01:17:08.000000Z
closed: 2026-04-28T01:19:03.123456Z
type: task
priority: 2
---
# Look through your recent changes to find the new theme


## Summary

Found a new "midnight" theme in your settings file. It defines colors for dark mode but isn't being registered with the theme switcher.
"""
    result = parse_ticket_text(text)
    assert result is not None
    assert result.status == "closed"
    assert result.summary is not None
    assert "midnight" in result.summary


def test_parse_summary_preserves_special_characters() -> None:
    """The summary is read verbatim, so characters that would break a
    sed-based frontmatter write -- slashes, ampersands, quotes -- survive
    intact. This is the reason the summary lives in its own section rather
    than a frontmatter field."""
    text = """---
id: tt-2efd
status: closed
created: 2026-04-28T01:17:08Z
closed: 2026-04-28T01:19:03.123456Z
---
# A task

## Summary

Fixed the read/write & "escaping" path: 100% done.
"""
    result = parse_ticket_text(text)
    assert result is not None
    assert result.summary == 'Fixed the read/write & "escaping" path: 100% done.'


def test_parse_reads_summary_section_not_notes() -> None:
    """A ticket may carry both an `add-note`-written `## Notes` section and a
    close-written `## Summary` section. Only the `## Summary` body is the
    summary; `## Notes` content is ignored."""
    text = """---
id: tt-2efd
status: closed
created: 2026-04-28T01:17:08Z
closed: 2026-04-28T01:20:00.000000Z
---
# A multi-step task

## Notes

**2026-04-28T01:18:00Z**

An interim observation that is not the close summary.

## Summary

The actual close summary.
"""
    result = parse_ticket_text(text)
    assert result is not None
    assert result.summary == "The actual close summary."


def test_parse_returns_none_for_no_frontmatter() -> None:
    assert parse_ticket_text("# Just a title with no frontmatter\n") is None


def test_parse_returns_none_for_missing_id() -> None:
    text = """---
status: open
created: 2026-04-28T01:17:08Z
---
# Title
"""
    assert parse_ticket_text(text) is None


def test_parse_returns_none_for_invalid_status() -> None:
    text = """---
id: tt-abcd
status: bogus
created: 2026-04-28T01:17:08Z
---
# Title
"""
    assert parse_ticket_text(text) is None


def test_title_falls_back_to_id_when_no_h1() -> None:
    text = """---
id: tt-noheader
status: open
created: 2026-04-28T01:17:08Z
---
Some body text without an H1 heading.
"""
    result = parse_ticket_text(text)
    assert result is not None
    assert result.title == "tt-noheader"


def test_closed_ticket_without_summary_section_has_no_summary() -> None:
    """A ticket closed with no summary argument has no `## Summary` section,
    so the parser reports no summary -- body prose is never mistaken for one."""
    text = """---
id: tt-prose
status: closed
created: 2026-04-28T01:17:08Z
---
# Title

Some body prose here that is not a summary.
"""
    result = parse_ticket_text(text)
    assert result is not None
    assert result.summary is None


def test_parse_captures_agent_field_when_present() -> None:
    """The patched `tk create` stamps the creating mngr agent's name into
    the frontmatter as `agent:`. The parser must surface it so the watcher
    can filter tickets that belong to a different agent."""
    text = """---
id: tt-stamp
status: open
deps: []
links: []
created: 2026-04-28T01:17:08Z
type: task
priority: 2
agent: crystallize-email-digest
---
# Stamped task
"""
    result = parse_ticket_text(text)
    assert result is not None
    assert result.agent == "crystallize-email-digest"


def test_parse_agent_field_defaults_to_empty_when_missing() -> None:
    """Tickets created before the stamping patch -- or by any tk invocation
    outside an mngr context -- have no `agent:` line. The parser returns
    an empty string for the field so the watcher's "absent = include for
    any agent" backwards-compat path works."""
    text = """---
id: tt-bare
status: open
deps: []
links: []
created: 2026-04-28T01:17:08Z
type: task
priority: 2
---
# Pre-stamping task
"""
    result = parse_ticket_text(text)
    assert result is not None
    assert result.agent == ""


def test_parse_captures_step_field() -> None:
    """`tk create --step` stamps `step: true` into the frontmatter to
    distinguish a turn-bound progress record from a regular ticket. The
    parser must surface this so the watcher's per-agent filter and the
    frontend's nesting logic can act on it."""
    text = """---
id: ts-step
status: open
deps: []
links: []
created: 2026-04-28T01:17:08Z
type: task
priority: 2
agent: agent-A
step: true
parent: tt-parent
---
# A step record
"""
    result = parse_ticket_text(text)
    assert result is not None
    assert result.step is True
    assert result.parent_id == "tt-parent"


def test_parse_step_defaults_to_false_when_absent() -> None:
    """Tickets without a `step:` line are regular tickets. The parser
    must default the field to False so the watcher's surfacing rule
    routes them via the assignee path rather than the creator path."""
    text = """---
id: tt-regular
status: open
created: 2026-04-28T01:17:08Z
---
# Regular ticket
"""
    result = parse_ticket_text(text)
    assert result is not None
    assert result.step is False
    assert result.parent_id == ""


def test_parse_step_accepts_case_variations() -> None:
    """The `step: True` / `step: TRUE` casing variants must all parse
    as boolean true. YAML allows arbitrary casing for booleans and
    different tooling (e.g. a hand-edited file) may emit either form."""
    for value in ("true", "True", "TRUE"):
        text = f"""---
id: tt-{value}
status: open
created: 2026-04-28T01:17:08Z
step: {value}
---
# Mixed case step
"""
        result = parse_ticket_text(text)
        assert result is not None, f"failed to parse for value {value!r}"
        assert result.step is True


def test_parse_captures_assignee_field() -> None:
    """`tk start` auto-self-assigns the running mngr agent; the
    `assignee:` value drives the watcher's per-agent surfacing rule for
    regular tickets, so the parser must surface it."""
    text = """---
id: tt-asg
status: in_progress
created: 2026-04-28T01:17:08Z
agent: agent-creator
assignee: agent-picker
---
# A picked-up ticket
"""
    result = parse_ticket_text(text)
    assert result is not None
    assert result.assignee == "agent-picker"
    assert result.agent == "agent-creator"


def test_parse_assignee_defaults_to_empty_string_when_absent() -> None:
    """Unassigned regular tickets (post-patch tk leaves the field unset
    in an mngr context until `tk start` runs) must parse with an empty
    assignee so the watcher's fallthrough branch can take over."""
    text = """---
id: tt-unasg
status: open
created: 2026-04-28T01:17:08Z
agent: agent-creator
---
# Filed but not picked up yet
"""
    result = parse_ticket_text(text)
    assert result is not None
    assert result.assignee == ""


def test_inline_summary_substring_does_not_anchor_summary_section() -> None:
    """A `## Summary` substring appearing mid-line must NOT be treated as the
    start of the Summary section. The real heading -- a line whose stripped
    contents are exactly `## Summary` -- is the only valid anchor; otherwise
    prose after a false-positive marker could leak in as the summary.
    """
    text = """---
id: tt-prose
status: closed
created: 2026-04-28T01:17:08Z
---
# Title

The user mentioned "## Summary" inline as part of their description, so
the parser must not anchor on that.

## Summary

Real summary in the real summary section.
"""
    result = parse_ticket_text(text)
    assert result is not None
    assert result.summary == "Real summary in the real summary section."
