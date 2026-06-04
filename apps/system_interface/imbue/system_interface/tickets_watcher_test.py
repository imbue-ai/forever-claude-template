"""Unit tests for AgentTicketsWatcher.

The watcher maintains a current ENRICHMENT snapshot of the agent's step
records (ticket_id -> {title, summary, status, created_at}), scoped per
session. It serves a scope via get_enrichment(session_id) and broadcasts a
`step_enrichment` message per changed scope (main untagged, subagents tagged).
Only step records surface; regular tickets are dropped.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from imbue.system_interface.step_attribution import StepAttribution
from imbue.system_interface.tickets_watcher import AgentTicketsWatcher
from imbue.system_interface.tickets_watcher import ENRICHMENT_MESSAGE_TYPE


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
    summary: str | None = None,
    agent: str | None = None,
    step: bool = True,
) -> str:
    """Build a tk-shaped ticket body. Defaults to a step record (the only kind
    the watcher surfaces) so individual tests only describe what varies.
    `summary` is written into a `## Summary` section, matching what
    `tk close <id> "summary"` emits."""
    agent_line = f"agent: {agent}\n" if agent is not None else ""
    step_line = "step: true\n" if step else ""
    body = f"""---
id: {ticket_id}
status: {status}
deps: []
links: []
created: {created}
type: task
priority: 2
{agent_line}{step_line}---
# {title}
"""
    if summary is not None:
        body += f"\n## Summary\n\n{summary}\n"
    return body


def _write_ticket(
    tickets_dir: Path,
    ticket_id: str,
    status: str,
    *,
    title: str = "Sample task",
    summary: str | None = None,
    agent: str | None = None,
    step: bool = True,
) -> Path:
    tickets_dir.mkdir(parents=True, exist_ok=True)
    path = tickets_dir / f"{ticket_id}.md"
    path.write_text(_ticket_text(ticket_id, status, title=title, summary=summary, agent=agent, step=step))
    return path


def test_silent_when_tickets_dir_missing(tmp_path: Path) -> None:
    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("agent-1", "agent-1-name", tmp_path / ".tickets", cb)
    assert watcher.get_enrichment() == {}


def test_scan_skips_files_with_invalid_utf8(tmp_path: Path) -> None:
    """A *.md file containing non-UTF-8 bytes must not crash the watcher; it is
    skipped silently like any other unreadable file."""
    tickets_dir = tmp_path / ".tickets"
    _write_ticket(tickets_dir, "tt-good", "open", title="Valid step")
    (tickets_dir / "tt-bad.md").write_bytes(b"---\nid: tt-bad\nstatus: open\nstep: true\n---\n# \xff\xfe\xfd\n")

    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("agent-1", "agent-1-name", tickets_dir, cb)
    enrichment = watcher.get_enrichment()
    assert list(enrichment.keys()) == ["tt-good"]


def test_open_step_snapshot_entry(tmp_path: Path) -> None:
    """A freshly-discovered open step appears in the snapshot with its title,
    status, and a normalised created_at; no summary while not closed."""
    tickets_dir = tmp_path / ".tickets"
    _write_ticket(tickets_dir, "tt-aaaa", "open", title="Hello world")
    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("agent-1", "agent-1-name", tickets_dir, cb)
    entry = watcher.get_enrichment()["tt-aaaa"]
    assert entry == {
        "title": "Hello world",
        "summary": None,
        "status": "open",
        # second-resolution frontmatter normalised to fixed-width microseconds
        "created_at": "2026-04-28T01:00:00.000000Z",
    }


def test_closed_step_carries_summary(tmp_path: Path) -> None:
    """A closed step's snapshot entry carries the `## Summary` text as summary."""
    tickets_dir = tmp_path / ".tickets"
    _write_ticket(
        tickets_dir,
        "tt-cccc",
        "closed",
        title="Done task",
        summary="Final summary text for this task.",
    )
    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("agent-1", "agent-1-name", tickets_dir, cb)
    entry = watcher.get_enrichment()["tt-cccc"]
    assert entry["status"] == "closed"
    assert entry["summary"] == "Final summary text for this task."


def test_summary_only_on_closed(tmp_path: Path) -> None:
    """An in_progress step with a Summary section does not leak it as a summary
    (only closed steps surface a summary)."""
    tickets_dir = tmp_path / ".tickets"
    _write_ticket(
        tickets_dir,
        "tt-dddd",
        "in_progress",
        title="Still working",
        summary="Interim text, not a final summary yet.",
    )
    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("agent-1", "agent-1-name", tickets_dir, cb)
    assert watcher.get_enrichment()["tt-dddd"]["summary"] is None


def test_get_enrichment_is_idempotent(tmp_path: Path) -> None:
    """Re-reading an unchanged directory yields the same snapshot."""
    tickets_dir = tmp_path / ".tickets"
    _write_ticket(tickets_dir, "tt-eeee", "open", title="Stable")
    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("agent-1", "agent-1-name", tickets_dir, cb)
    first = watcher.get_enrichment()
    second = watcher.get_enrichment()
    assert first == second
    assert list(first.keys()) == ["tt-eeee"]


def test_lifecycle_updates_snapshot_status(tmp_path: Path) -> None:
    """As a step moves open -> in_progress -> closed, the snapshot entry's
    status (and summary on close) tracks the latest file state."""
    tickets_dir = tmp_path / ".tickets"
    path = _write_ticket(tickets_dir, "tt-ffff", "open", title="Lifecycle")
    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("agent-1", "agent-1-name", tickets_dir, cb)
    assert watcher.get_enrichment()["tt-ffff"]["status"] == "open"

    path.write_text(_ticket_text("tt-ffff", "in_progress", title="Lifecycle"))
    assert watcher.get_enrichment()["tt-ffff"]["status"] == "in_progress"

    path.write_text(_ticket_text("tt-ffff", "closed", title="Lifecycle", summary="All done."))
    final = watcher.get_enrichment()["tt-ffff"]
    assert final["status"] == "closed"
    assert final["summary"] == "All done."


def test_created_at_falls_back_to_mtime_when_field_absent(tmp_path: Path) -> None:
    """A step with no `created:` frontmatter still gets a sortable created_at:
    the file's mtime, in the uniform microsecond format -- never empty, which
    would sort to the front of the pending tail."""
    tickets_dir = tmp_path / ".tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)
    path = tickets_dir / "tt-nocreate.md"
    path.write_text("---\nid: tt-nocreate\nstatus: open\nstep: true\n---\n# No created field\n")
    mtime = 1_777_000_000.123456
    os.utime(path, (mtime, mtime))

    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("agent-1", "agent-1-name", tickets_dir, cb)
    expected = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    assert watcher.get_enrichment()["tt-nocreate"]["created_at"] == expected


def test_created_at_falls_back_to_mtime_when_field_malformed(tmp_path: Path) -> None:
    """A malformed `created:` value must not reach the snapshot verbatim: it would
    break the lexicographic `created_at` sort the frontend uses to order pending
    steps. The watcher falls back to the file mtime, same as the absent case."""
    tickets_dir = tmp_path / ".tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)
    path = tickets_dir / "tt-badts.md"
    path.write_text(
        "---\nid: tt-badts\nstatus: open\nstep: true\ncreated: not-a-timestamp\n---\n# Bad created field\n"
    )
    mtime = 1_777_000_000.123456
    os.utime(path, (mtime, mtime))

    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("agent-1", "agent-1-name", tickets_dir, cb)
    expected = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    assert watcher.get_enrichment()["tt-badts"]["created_at"] == expected


def test_regular_tickets_are_dropped(tmp_path: Path) -> None:
    """Regular (non-step) tickets are a separate construct and never appear in
    the progress view's enrichment."""
    tickets_dir = tmp_path / ".tickets"
    _write_ticket(tickets_dir, "ts-step", "open", title="A step", step=True)
    _write_ticket(tickets_dir, "tt-regular", "open", title="A regular ticket", step=False)
    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("agent-1", "agent-1-name", tickets_dir, cb)
    assert list(watcher.get_enrichment().keys()) == ["ts-step"]


def test_step_surfaces_only_to_its_creator(tmp_path: Path) -> None:
    """Step records are creator-private: a step stamped with a sibling agent's
    name must not leak into this agent's snapshot (the bug two agents sharing a
    TICKETS_DIR would otherwise hit)."""
    tickets_dir = tmp_path / ".tickets"
    _write_ticket(tickets_dir, "ts-mine", "open", title="My step", agent="agent-A")
    _write_ticket(tickets_dir, "ts-theirs", "open", title="Their step", agent="agent-B")
    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("agent-A-id", "agent-A", tickets_dir, cb)
    assert list(watcher.get_enrichment().keys()) == ["ts-mine"]


def test_unstamped_step_surfaces_to_everyone(tmp_path: Path) -> None:
    """A step with no `agent:` stamp (older tk) surfaces to every agent's
    watcher so a rollout doesn't make historical steps vanish."""
    tickets_dir = tmp_path / ".tickets"
    _write_ticket(tickets_dir, "ts-bare", "open", title="Legacy step")
    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("any-id", "any-agent", tickets_dir, cb)
    assert list(watcher.get_enrichment().keys()) == ["ts-bare"]


def test_get_enrichment_broadcasts_snapshot_on_change(tmp_path: Path) -> None:
    """get_enrichment() forwards a single `step_enrichment` snapshot message
    through on_events when its catch-up scan finds a change, so live SSE
    subscribers update without a refetch; an unchanged scan is a callback
    no-op."""
    tickets_dir = tmp_path / ".tickets"
    path = _write_ticket(tickets_dir, "ts-cb", "open", title="First")
    calls, cb = _capture()
    watcher = AgentTicketsWatcher("agent-1", "agent-1-name", tickets_dir, cb)

    # First read discovers ts-cb -> one broadcast carrying the snapshot.
    watcher.get_enrichment()
    assert len(calls) == 1
    agent_id, events = calls[0]
    assert agent_id == "agent-1"
    assert len(events) == 1
    msg = events[0]
    assert msg["type"] == ENRICHMENT_MESSAGE_TYPE
    assert msg["enrichment"]["ts-cb"]["status"] == "open"

    # No change -> no new broadcast.
    watcher.get_enrichment()
    assert len(calls) == 1

    # A change -> another broadcast with the updated snapshot.
    path.write_text(_ticket_text("ts-cb", "in_progress", title="First"))
    watcher.get_enrichment()
    assert len(calls) == 2
    assert calls[1][1][0]["enrichment"]["ts-cb"]["status"] == "in_progress"


def test_concurrent_reads_do_not_decouple_broadcast_from_change(tmp_path: Path) -> None:
    """Under concurrent get_enrichment() calls (request handlers racing the poll
    thread), each broadcast must reflect the change it detected. The split
    detect-then-copy form let a concurrent scan advance the snapshot between
    detection and copy, so the same state could be broadcast twice while an
    intermediate state was skipped. Invariant guarded here: no two CONSECUTIVE
    broadcasts carry the same status -- impossible on the atomic code (a second
    scan sees no change), reproduced by the decoupling bug. The driver only ever
    writes a status different from the previous one, so a duplicate can only come
    from decoupling, not from a real repeat."""
    tickets_dir = tmp_path / ".tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)
    md = tickets_dir / "tt-race.md"
    tmp = tickets_dir / "tt-race.tmp"

    def write(status: str) -> None:
        # Write atomically (temp + rename) so a concurrent scan never reads a
        # half-written file -- isolates the test to the decoupling race.
        tmp.write_text(_ticket_text("tt-race", status, title="Race"))
        tmp.rename(md)

    write("open")
    lock = threading.Lock()
    broadcasts: list[str | None] = []

    def cb(_agent_id: str, events: list[dict[str, Any]]) -> None:
        with lock:
            for e in events:
                broadcasts.append(e["enrichment"].get("tt-race", {}).get("status"))

    watcher = AgentTicketsWatcher("a", "a-name", tickets_dir, cb)
    stop = threading.Event()
    # Release readers and the writer together for maximum contention, with no
    # sleeps: the GIL interleaves the tight scan loops with the writer's file
    # I/O. The invariant holds for ANY interleaving on the atomic code, so the
    # test never flaky-fails; it only fails if the decoupling race is present.
    n_readers = 4
    gate = threading.Barrier(n_readers + 1)

    def reader() -> None:
        gate.wait()
        while not stop.is_set():
            watcher.get_enrichment()

    readers = [threading.Thread(target=reader) for _ in range(n_readers)]
    for r in readers:
        r.start()
    gate.wait()
    # Each successive status differs from the previous one (no real repeats).
    for status in ["in_progress", "closed", "open"] * 40 + ["in_progress", "closed"]:
        write(status)
    stop.set()
    for r in readers:
        r.join()
    # Final drain so the terminal state is observed.
    watcher.get_enrichment()

    with lock:
        seen = list(broadcasts)
    for i in range(1, len(seen)):
        assert seen[i] != seen[i - 1], f"consecutive duplicate broadcast at {i}: {seen}"


def test_created_without_timezone_is_assumed_utc(tmp_path: Path) -> None:
    """A `created` value lacking a timezone offset is normalised as UTC, not
    shifted by the machine's local offset (which is what a naive
    astimezone() would do on a non-UTC host)."""
    tickets_dir = tmp_path / ".tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)
    (tickets_dir / "tt-naive.md").write_text(
        _ticket_text("tt-naive", "open", title="Naive", created="2026-04-28T01:00:00")
    )
    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("agent-1", "agent-1-name", tickets_dir, cb)
    assert watcher.get_enrichment()["tt-naive"]["created_at"] == "2026-04-28T01:00:00.000000Z"


# --- Per-session scoping (subagent steps must not leak into the main view) ---


def _fixed_attribution(attribution: StepAttribution) -> Callable[[], StepAttribution]:
    """A provider returning a fixed attribution, standing in for the session
    watcher in scoping tests so they don't need a live transcript."""

    def provider() -> StepAttribution:
        return attribution

    return provider


def test_subagent_steps_scoped_out_of_main_view(tmp_path: Path) -> None:
    """Steps a subagent created (and started/closed) in the SHARED .tickets dir
    under the SAME agent name must not appear in the main view's enrichment;
    they surface only under the subagent session's scope. This is the core bug:
    on `main`, all four steps commingle in one dir stamped with one agent name."""
    tickets_dir = tmp_path / ".tickets"
    # Two main steps (started/closed in the main session) and two subagent steps.
    _write_ticket(tickets_dir, "cod-main1", "closed", title="Main work one", agent="wolf", summary="done1")
    _write_ticket(tickets_dir, "cod-main2", "in_progress", title="Main work two", agent="wolf")
    _write_ticket(tickets_dir, "cod-sub1", "closed", title="Sub work one", agent="wolf", summary="subdone1")
    _write_ticket(tickets_dir, "cod-sub2", "open", title="Sub pending", agent="wolf")

    attribution = StepAttribution(
        transition_ids_by_session={
            "main-sess": ("cod-main1", "cod-main2"),
            "agent-sub": ("cod-sub1",),
        },
        # cod-sub2 is pending: matched to the subagent by its create title.
        create_titles_by_session={"agent-sub": ("Sub pending",)},
        main_session_ids=("main-sess",),
    )

    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("wolf-id", "wolf", tickets_dir, cb, attribution_provider=_fixed_attribution(attribution))

    main = watcher.get_enrichment()
    assert sorted(main.keys()) == ["cod-main1", "cod-main2"]

    sub = watcher.get_enrichment(session_id="agent-sub")
    assert sorted(sub.keys()) == ["cod-sub1", "cod-sub2"]
    # Enrichment content still travels with whichever scope owns the step.
    assert sub["cod-sub1"]["summary"] == "subdone1"
    assert main["cod-main1"]["summary"] == "done1"


def test_scope_broadcasts_are_tagged_per_session(tmp_path: Path) -> None:
    """On change, the main scope broadcasts an untagged step_enrichment message
    (so the existing main-stream filter keeps it) while a subagent scope is
    tagged with its session id (so the per-subagent filter routes it)."""
    tickets_dir = tmp_path / ".tickets"
    _write_ticket(tickets_dir, "cod-main1", "in_progress", title="Main", agent="wolf")
    _write_ticket(tickets_dir, "cod-sub1", "in_progress", title="Sub", agent="wolf")
    attribution = StepAttribution(
        transition_ids_by_session={"main-sess": ("cod-main1",), "agent-sub": ("cod-sub1",)},
        create_titles_by_session={},
        main_session_ids=("main-sess",),
    )

    calls, cb = _capture()
    watcher = AgentTicketsWatcher("wolf-id", "wolf", tickets_dir, cb, attribution_provider=_fixed_attribution(attribution))
    watcher.get_enrichment()

    messages = [msg for _agent_id, events in calls for msg in events]
    by_scope = {msg.get("session_id"): msg for msg in messages}
    # Main scope: untagged (no session_id), carries only the main step.
    assert None in by_scope
    assert list(by_scope[None]["enrichment"].keys()) == ["cod-main1"]
    # Subagent scope: tagged with its session id, carries only the subagent step.
    assert "agent-sub" in by_scope
    assert list(by_scope["agent-sub"]["enrichment"].keys()) == ["cod-sub1"]
    assert by_scope["agent-sub"]["type"] == ENRICHMENT_MESSAGE_TYPE


def test_no_attribution_provider_keeps_everything_in_main(tmp_path: Path) -> None:
    """Without a provider (single-agent path, and the path tests without a
    session watcher take), every step stays in the main scope and a subagent
    query returns nothing."""
    tickets_dir = tmp_path / ".tickets"
    _write_ticket(tickets_dir, "cod-a", "open", title="A", agent="wolf")
    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("wolf-id", "wolf", tickets_dir, cb)
    assert list(watcher.get_enrichment().keys()) == ["cod-a"]
    assert watcher.get_enrichment(session_id="agent-sub") == {}
