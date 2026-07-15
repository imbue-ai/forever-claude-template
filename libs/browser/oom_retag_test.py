"""Tests for the Chromium oom_score_adj re-tagging sweep.

The sweep policy is tested with injected collaborators (a recorded fake
``/proc``), so no real process tree or writable ``/proc`` is needed. What
matters: Chrome-lowered values are remapped into the browser band preserving
their order, everything at/above the floor (inherited-ceiling processes,
already-remapped processes) is untouched, and repeating the sweep writes
nothing more. The scheduler is tested with an injected sweep and tight
timings: it must be idle until kicked, and one kick must produce a burst of
sweeps (not just one), since the processes spawn after the triggering event.
"""

import threading

from browser.oom_retag import RetagScheduler, sweep_once
from oom_priority import bands


class _FakeProc:
    """Records adj reads/writes against a fixed initial state."""

    def __init__(self, initial: dict[int, int]) -> None:
        self.adj = dict(initial)
        self.writes: list[tuple[int, int]] = []

    def read(self, pid: int) -> int | None:
        return self.adj.get(pid)

    def write(self, pid: int, adj: int) -> bool:
        self.adj[pid] = adj
        self.writes.append((pid, adj))
        return True


def _sweep(proc: _FakeProc, descendants: list[int]) -> list[tuple[int, int, int]]:
    return sweep_once(
        1,
        read_adj=proc.read,
        write_adj=proc.write,
        list_descendants=lambda pid: descendants,
    )


def test_chrome_lowered_values_are_remapped_into_the_band_preserving_order() -> None:
    # A realistic post-launch Chromium tree: main 0, gpu/utility 200, renderers
    # 300 -- plus the node driver and crashpad still at the inherited ceiling.
    proc = _FakeProc({10: 0, 11: 200, 12: 300, 13: 300, 20: 1000, 21: 1000})
    writes = _sweep(proc, [10, 11, 12, 13, 20, 21])
    assert [pid for pid, _, _ in writes] == [10, 11, 12, 13]
    for _, old, new in writes:
        assert new == bands.shared_browser_oom_score_adj(old)
        assert bands.SHARED_BROWSER_FLOOR <= new <= bands.SHARED_BROWSER
    # Chrome's ordering survives: main < gpu/utility < renderers.
    assert proc.adj[10] < proc.adj[11] < proc.adj[12] == proc.adj[13]
    # The inherited-ceiling processes are untouched.
    assert proc.adj[20] == proc.adj[21] == 1000


def test_repeating_the_sweep_writes_nothing_more() -> None:
    proc = _FakeProc({10: 0, 11: 200, 12: 300})
    _sweep(proc, [10, 11, 12])
    assert _sweep(proc, [10, 11, 12]) == []


def test_exited_processes_are_skipped() -> None:
    # pid 11 exited between the walk and the read: its adj is unreadable.
    proc = _FakeProc({10: 0})
    writes = _sweep(proc, [10, 11])
    assert [pid for pid, _, _ in writes] == [10]


def test_scheduler_is_idle_until_kicked_then_sweeps() -> None:
    swept = threading.Event()
    scheduler = RetagScheduler(sweep=swept.set, burst_interval=0.01, burst_duration=0.1)
    scheduler.start()
    try:
        assert not swept.wait(timeout=0.05), "swept before any kick"
        scheduler.kick()
        assert swept.wait(timeout=2.0), "kick did not trigger a sweep"
    finally:
        scheduler.stop()


def test_one_kick_produces_a_burst_of_sweeps() -> None:
    # The Chromium processes spawn (and self-write their values) over the
    # seconds after the triggering event, so a single immediate sweep would
    # race them: one kick must keep re-sweeping through the burst window.
    sweep_count = [0]
    third_sweep = threading.Event()

    def record_sweep() -> None:
        sweep_count[0] += 1
        if sweep_count[0] >= 3:
            third_sweep.set()

    scheduler = RetagScheduler(
        sweep=record_sweep, burst_interval=0.01, burst_duration=5.0
    )
    scheduler.start()
    try:
        scheduler.kick()
        assert third_sweep.wait(timeout=2.0), "the burst did not re-sweep"
    finally:
        scheduler.stop()
