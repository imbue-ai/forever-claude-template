"""Unit tests for AgentTicketsWatcher.

The watcher emits one event per OBSERVED state transition. On replay
(the watcher is started against a directory whose tickets are already
past-`open`), only the current status emits an event -- earlier
transitions weren't observed and are not synthesized.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from imbue.system_interface.tickets_watcher import AgentTicketsWatcher


def _capture() -> tuple[list[tuple[str, list[dict[str, Any]]]], Any]:
    """Returns (calls, callback) for use as the watcher's on_events arg."""
    calls: list[tuple[str, list[dict[str, Any]]]] = []

    def cb(agent_id: str, events: list[dict[str, Any]]) -> None:
        calls.append((agent_id, events))

    return calls, cb


def _ticket_text(
    ticket_id: str,
    status: str,
    *,
    title: str = "Sample task",
    created: str = "2026-04-28T01:00:00Z",
    notes: str | None = None,
    agent: str | None = None,
    step: bool = False,
    parent: str | None = None,
    assignee: str | None = None,
) -> str:
    """Build a tk-shaped ticket body. Centralizes the boilerplate frontmatter
    so individual tests only describe the parts that actually vary."""
    agent_line = f"agent: {agent}\n" if agent is not None else ""
    step_line = "step: true\n" if step else ""
    parent_line = f"parent: {parent}\n" if parent is not None else ""
    assignee_line = f"assignee: {assignee}\n" if assignee is not None else ""
    body = f"""---
id: {ticket_id}
status: {status}
deps: []
links: []
created: {created}
type: task
priority: 2
{assignee_line}{agent_line}{step_line}{parent_line}---
# {title}
"""
    if notes is not None:
        body += f"\n## Notes\n\n{notes}\n"
    return body


def _write_ticket_with_status(
    tickets_dir: Path,
    ticket_id: str,
    status: str,
    *,
    title: str = "Sample task",
    notes: str | None = None,
    agent: str | None = None,
    step: bool = False,
    parent: str | None = None,
    assignee: str | None = None,
) -> Path:
    tickets_dir.mkdir(parents=True, exist_ok=True)
    path = tickets_dir / f"{ticket_id}.md"
    path.write_text(
        _ticket_text(
            ticket_id,
            status,
            title=title,
            notes=notes,
            agent=agent,
            step=step,
            parent=parent,
            assignee=assignee,
        )
    )
    return path


def test_silent_when_tickets_dir_missing(tmp_path: Path) -> None:
    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("agent-1", "agent-1-name", tmp_path / ".tickets", cb)
    assert watcher.get_all_events() == []


def test_scan_skips_files_with_invalid_utf8(tmp_path: Path) -> None:
    """A *.md file containing non-UTF-8 bytes must not crash the watcher;
    it should be skipped silently like any other unreadable file. Without
    this, a single malformed file would propagate UnicodeDecodeError up
    through _scan() and kill the watcher's background thread."""
    tickets_dir = tmp_path / ".tickets"
    _write_ticket_with_status(tickets_dir, "tt-good", "open", title="Valid ticket")
    bad_file = tickets_dir / "tt-bad.md"
    bad_file.write_bytes(b"---\nid: tt-bad\nstatus: open\n---\n# \xff\xfe\xfd not utf-8\n")

    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("agent-1", "agent-1-name", tickets_dir, cb)
    events = watcher.get_all_events()
    # The valid ticket comes through; the malformed one is silently skipped.
    assert [e["ticket_id"] for e in events] == ["tt-good"]


def test_open_ticket_emits_one_event_with_created_at_timestamp(tmp_path: Path) -> None:
    """A freshly-discovered open ticket emits a single open event whose
    timestamp comes from the frontmatter `created` field (truthful)."""
    tickets_dir = tmp_path / ".tickets"
    _write_ticket_with_status(tickets_dir, "tt-aaaa", "open", title="Hello world")
    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("agent-1", "agent-1-name", tickets_dir, cb)
    events = watcher.get_all_events()
    assert len(events) == 1
    assert events[0]["event_id"] == "tt-aaaa-open"
    assert events[0]["status"] == "open"
    assert events[0]["timestamp"] == "2026-04-28T01:00:00Z"
    assert events[0]["title"] == "Hello world"


def test_replayed_in_progress_ticket_emits_only_current_status(tmp_path: Path) -> None:
    """A ticket discovered already at in_progress was not observed
    transitioning from open -- so we emit a single in_progress event,
    NOT a synthetic open event."""
    tickets_dir = tmp_path / ".tickets"
    _write_ticket_with_status(tickets_dir, "tt-bbbb", "in_progress", title="In progress task")
    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("agent-1", "agent-1-name", tickets_dir, cb)
    events = watcher.get_all_events()
    assert len(events) == 1
    assert events[0]["event_id"] == "tt-bbbb-in_progress"
    # created_at field still carries the frontmatter value -- the
    # frontend uses that for turn attribution and the "ticket existed
    # since" lower bound.
    assert events[0]["created_at"] == "2026-04-28T01:00:00Z"


def test_replayed_closed_ticket_emits_only_closed_event_with_summary(tmp_path: Path) -> None:
    """A ticket discovered already at closed emits one closed event;
    no synthetic in_progress is generated. Summary still rides on the
    closed event."""
    tickets_dir = tmp_path / ".tickets"
    _write_ticket_with_status(
        tickets_dir,
        "tt-cccc",
        "closed",
        title="Done task",
        notes="**2026-04-28T01:05:00Z**\n\nFinal summary text for this task.",
    )
    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("agent-1", "agent-1-name", tickets_dir, cb)
    events = watcher.get_all_events()
    assert len(events) == 1
    assert events[0]["event_id"] == "tt-cccc-closed"
    assert events[0]["status"] == "closed"
    assert events[0]["summary"] == "Final summary text for this task."
    assert events[0]["summary_at"] == "2026-04-28T01:05:00Z"


def test_summary_only_on_closed_event(tmp_path: Path) -> None:
    """A ticket with notes still in_progress: no summary leaks; the
    in_progress event's summary field is None."""
    tickets_dir = tmp_path / ".tickets"
    _write_ticket_with_status(
        tickets_dir,
        "tt-dddd",
        "in_progress",
        title="Still working",
        notes="**2026-04-28T01:02:00Z**\n\nInterim note that should not appear as a summary yet.",
    )
    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("agent-1", "agent-1-name", tickets_dir, cb)
    events = watcher.get_all_events()
    assert len(events) == 1
    assert events[0]["summary"] is None
    assert events[0]["summary_at"] is None


def test_repeated_get_all_events_is_idempotent(tmp_path: Path) -> None:
    """Re-calling get_all_events() against an unchanged directory yields
    the same cumulative history. This is the contract _get_combined_events
    in server.py relies on: every page reload re-issues GET /events and
    expects the full event list back, not just deltas since the last poll."""
    tickets_dir = tmp_path / ".tickets"
    _write_ticket_with_status(tickets_dir, "tt-eeee", "open", title="Stable")
    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("agent-1", "agent-1-name", tickets_dir, cb)
    first = watcher.get_all_events()
    assert [e["event_id"] for e in first] == ["tt-eeee-open"]
    second = watcher.get_all_events()
    assert second == first


def test_lifecycle_accumulates_one_event_per_observed_transition(tmp_path: Path) -> None:
    """A ticket the watcher observes through its full lifecycle (open
    -> in_progress -> closed) accumulates exactly three events in the
    cumulative history, one per observed transition. get_all_events()
    returns the full accumulated list each call."""
    tickets_dir = tmp_path / ".tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)
    path = tickets_dir / "tt-ffff.md"

    path.write_text(_ticket_text("tt-ffff", "open", title="Lifecycle test"))

    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("agent-1", "agent-1-name", tickets_dir, cb)

    events1 = watcher.get_all_events()
    assert [e["event_id"] for e in events1] == ["tt-ffff-open"]

    path.write_text(_ticket_text("tt-ffff", "in_progress", title="Lifecycle test"))
    events2 = watcher.get_all_events()
    assert [e["event_id"] for e in events2] == ["tt-ffff-open", "tt-ffff-in_progress"]

    path.write_text(
        _ticket_text(
            "tt-ffff",
            "closed",
            title="Lifecycle test",
            notes="**2026-04-28T01:10:00Z**\n\nAll done.",
        )
    )
    events3 = watcher.get_all_events()
    assert [e["event_id"] for e in events3] == ["tt-ffff-open", "tt-ffff-in_progress", "tt-ffff-closed"]
    assert events3[-1]["summary"] == "All done."


def test_filters_out_tickets_stamped_with_a_different_agent(tmp_path: Path) -> None:
    """When workers share a TICKETS_DIR with their lead (the default minds
    setup), each agent's watcher must only surface tickets stamped with
    its own MNGR_AGENT_NAME. Tickets stamped with a sibling agent's name
    are silently skipped so they don't pollute the lead's progress view.
    """
    tickets_dir = tmp_path / ".tickets"
    _write_ticket_with_status(tickets_dir, "td-own", "open", title="Lead task", agent="lead-agent")
    _write_ticket_with_status(
        tickets_dir,
        "tw-sibling",
        "open",
        title="Worker task",
        agent="worker-agent",
    )

    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("lead-id", "lead-agent", tickets_dir, cb)
    events = watcher.get_all_events()
    assert [e["ticket_id"] for e in events] == ["td-own"]


def test_includes_unstamped_tickets_for_backwards_compatibility(tmp_path: Path) -> None:
    """Ticket files written before the stamping patch (and ticket files
    written by any tk invocation outside an mngr context) have no `agent:`
    line. They are kept for every agent's watcher -- attributing them to
    whoever is looking is the least-surprising behaviour and keeps the
    rollout from making historical tickets disappear from the chat."""
    tickets_dir = tmp_path / ".tickets"
    _write_ticket_with_status(tickets_dir, "tu-bare", "open", title="Legacy task")
    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("lead-id", "lead-agent", tickets_dir, cb)
    events = watcher.get_all_events()
    assert [e["ticket_id"] for e in events] == ["tu-bare"]


def test_get_all_events_forwards_newly_emitted_events_through_callback(tmp_path: Path) -> None:
    """`get_all_events()` documents that any transitions discovered by its
    catch-up scan are forwarded through `on_events`, so other connected
    websocket subscribers don't silently miss those events. Without this
    contract a request-handler scan would mark transitions "seen" in the
    watcher's cache before the background `_run` loop got a chance to
    observe them."""
    tickets_dir = tmp_path / ".tickets"
    path = _write_ticket_with_status(tickets_dir, "tt-cb-1", "open", title="First")
    calls, cb = _capture()
    watcher = AgentTicketsWatcher("agent-1", "agent-1-name", tickets_dir, cb)

    # First call discovers tt-cb-1 -> one callback invocation with one event.
    watcher.get_all_events()
    assert len(calls) == 1
    assert calls[0][0] == "agent-1"
    assert [e["event_id"] for e in calls[0][1]] == ["tt-cb-1-open"]

    # No new transitions: the next call is a no-op for the callback.
    watcher.get_all_events()
    assert len(calls) == 1

    # A new transition triggers another forward.
    path.write_text(_ticket_text("tt-cb-1", "in_progress", title="First"))
    watcher.get_all_events()
    assert len(calls) == 2
    assert [e["event_id"] for e in calls[1][1]] == ["tt-cb-1-in_progress"]


def test_step_record_surfaces_only_to_its_creator(tmp_path: Path) -> None:
    """Step records (`step: true`) are turn-bound progress markers and
    must NEVER leak across agents -- two agents sharing TICKETS_DIR are
    the precise bug this rule fixes. Even if the step's assignee field
    happens to match a sibling agent, it stays creator-scoped."""
    tickets_dir = tmp_path / ".tickets"
    _write_ticket_with_status(
        tickets_dir, "ts-mine", "open", title="My step", agent="agent-A", step=True
    )
    _write_ticket_with_status(
        tickets_dir, "ts-theirs", "open", title="Their step", agent="agent-B", step=True
    )

    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("agent-A-id", "agent-A", tickets_dir, cb)
    events = watcher.get_all_events()
    assert [e["ticket_id"] for e in events] == ["ts-mine"]
    assert events[0]["step"] is True


def test_regular_ticket_surfaces_to_assignee_not_creator(tmp_path: Path) -> None:
    """Regular tickets (no `step`) surface to their CURRENT assignee.
    A ticket created by A and picked up by B (assignee=B) shows in B's
    progress view, not A's. This is what makes ticket pickup work."""
    tickets_dir = tmp_path / ".tickets"
    _write_ticket_with_status(
        tickets_dir,
        "tt-picked",
        "in_progress",
        title="A's idea, B's work",
        agent="agent-A",
        assignee="agent-B",
    )

    _calls_a, cb_a = _capture()
    watcher_a = AgentTicketsWatcher("agent-A-id", "agent-A", tickets_dir, cb_a)
    assert watcher_a.get_all_events() == []

    _calls_b, cb_b = _capture()
    watcher_b = AgentTicketsWatcher("agent-B-id", "agent-B", tickets_dir, cb_b)
    events = watcher_b.get_all_events()
    assert [e["ticket_id"] for e in events] == ["tt-picked"]
    assert events[0]["assignee"] == "agent-B"


def test_unassigned_ticket_still_surfaces_to_creator_until_picked_up(tmp_path: Path) -> None:
    """A filed-but-not-yet-picked-up ticket has `agent:` set (creator)
    and `assignee:` empty/unset. It should surface to the creator's
    progress view while it waits -- once another agent runs `tk start`
    on it (setting assignee), it stops surfacing to the creator and
    starts surfacing to the picker. This is the routing handoff."""
    tickets_dir = tmp_path / ".tickets"
    _write_ticket_with_status(
        tickets_dir, "tt-filed", "open", title="Filed", agent="agent-A"
    )

    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("agent-A-id", "agent-A", tickets_dir, cb)
    events = watcher.get_all_events()
    assert [e["ticket_id"] for e in events] == ["tt-filed"]


def test_event_carries_step_parent_id_and_assignee_fields(tmp_path: Path) -> None:
    """The frontend's turn-grouping needs step / parent_id / assignee on
    every task_event to decide nesting and attribution. Verify the
    watcher actually stamps them into _make_event's payload."""
    tickets_dir = tmp_path / ".tickets"
    _write_ticket_with_status(
        tickets_dir,
        "ts-child",
        "open",
        title="Child step",
        agent="agent-A",
        step=True,
        parent="tt-parent",
    )

    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("agent-A-id", "agent-A", tickets_dir, cb)
    events = watcher.get_all_events()
    assert len(events) == 1
    e = events[0]
    assert e["step"] is True
    assert e["parent_id"] == "tt-parent"
    # assignee may be empty for steps -- they aren't part of the
    # pickup-handoff flow -- but the field must still be present.
    assert e["assignee"] == ""
