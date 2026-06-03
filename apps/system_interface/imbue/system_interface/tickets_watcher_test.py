"""Unit tests for AgentTicketsWatcher.

The watcher maintains a current ENRICHMENT snapshot of the agent's step
records (ticket_id -> {title, summary, status, created_at}). It serves the
snapshot via get_enrichment() and broadcasts a single `step_enrichment`
message whenever the snapshot changes. Only step records surface; regular
tickets are dropped.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

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
    notes: str | None = None,
    agent: str | None = None,
    step: bool = True,
) -> str:
    """Build a tk-shaped ticket body. Defaults to a step record (the only kind
    the watcher surfaces) so individual tests only describe what varies."""
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
    if notes is not None:
        body += f"\n## Notes\n\n{notes}\n"
    return body


def _write_ticket(
    tickets_dir: Path,
    ticket_id: str,
    status: str,
    *,
    title: str = "Sample task",
    notes: str | None = None,
    agent: str | None = None,
    step: bool = True,
) -> Path:
    tickets_dir.mkdir(parents=True, exist_ok=True)
    path = tickets_dir / f"{ticket_id}.md"
    path.write_text(_ticket_text(ticket_id, status, title=title, notes=notes, agent=agent, step=step))
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
    """A closed step's snapshot entry carries the most-recent note as summary."""
    tickets_dir = tmp_path / ".tickets"
    _write_ticket(
        tickets_dir,
        "tt-cccc",
        "closed",
        title="Done task",
        notes="**2026-04-28T01:05:00Z**\n\nFinal summary text for this task.",
    )
    _calls, cb = _capture()
    watcher = AgentTicketsWatcher("agent-1", "agent-1-name", tickets_dir, cb)
    entry = watcher.get_enrichment()["tt-cccc"]
    assert entry["status"] == "closed"
    assert entry["summary"] == "Final summary text for this task."


def test_summary_only_on_closed(tmp_path: Path) -> None:
    """An in_progress step with notes does not leak the note as a summary."""
    tickets_dir = tmp_path / ".tickets"
    _write_ticket(
        tickets_dir,
        "tt-dddd",
        "in_progress",
        title="Still working",
        notes="**2026-04-28T01:02:00Z**\n\nInterim note, not a summary yet.",
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

    path.write_text(_ticket_text("tt-ffff", "closed", title="Lifecycle", notes="**2026-04-28T01:10:00Z**\n\nAll done."))
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
