"""Keep the browser process tree in the shared-browser memory-shedding band.

The browser daemon is tagged to the top band (``SHARED_BROWSER``) at spawn and
everything it launches inherits that -- except Chromium, which deliberately
overwrites the inherited ``oom_score_adj`` once per process at startup with its
own internal gradation (browser/zygote 0, gpu/utility 200, renderers 300). Left
alone, that would make the memory-heavy renderers *more* protected than the
agents whose work they serve, inverting the shedding order.

The kernel cannot forbid that lowering without ``CAP_SYS_RESOURCE``, but
Chromium writes each value exactly once (its periodic re-adjustment is
ChromeOS-only), so an external raise sticks. The remapping preserves Chromium's
relative ordering in compressed form (the gradation is worth keeping: shedding
one renderer kills one tab, not the whole browser), only ever raises, and never
touches a value already at or above the floor -- so the node/Playwright driver
(inherited ceiling), crashpad (inherited ceiling), and already-remapped
processes are left alone, and repeated sweeps are idempotent.

The sweep is purely event-driven: new Chromium processes appear only at moments
the fleet can observe -- a browser launch, a new page (the CDP observer's
``page`` event fires for *every* new tab, whether an agent command, a human in
the cast viewer, or a page-initiated popup opened it), and a navigation (a
cross-site navigation can swap in a fresh renderer; ``framenavigated`` fires
for every frame, human- or agent-driven). ``session.py`` reports each of those
via :func:`notify_chromium_processes_expected`, which triggers a short *burst*
of sweeps -- the processes spawn (and Chrome self-writes their values) over the
seconds following the event, so a single immediate sweep would race it. Between
events the sweep thread sleeps indefinitely.

Runs on a plain daemon thread rather than the bridge's asyncio loop: it touches
no session state (only ``/proc``), and must keep working even when the loop is
busy driving browsers -- which is exactly when memory pressure peaks.
"""

import os
import threading
import time
from collections.abc import Callable

from loguru import logger
from oom_priority import bands
from oom_priority.proctree import list_descendant_pids

# After an event, keep sweeping at this cadence until the burst window closes.
# Chromium forks its processes (and each self-writes its oom_score_adj) within
# the first seconds after the triggering event; the window is generous so even
# a slow launch under load is covered, and each event extends it afresh.
_BURST_SWEEP_INTERVAL_SECONDS = 1.0
_BURST_DURATION_SECONDS = 6.0


def sweep_once(
    root_pid: int,
    read_adj: Callable[[int], int | None] = bands.read_oom_score_adj,
    write_adj: Callable[[int, int], bool] = bands.set_oom_score_adj,
    list_descendants: Callable[[int], list[int]] = list_descendant_pids,
) -> list[tuple[int, int, int]]:
    """Remap every descendant of ``root_pid`` sitting below the browser-band
    floor into the band's range; return ``(pid, old, new)`` per write.

    A pid whose value is unreadable (it exited, or there is no ``/proc``) is
    skipped, as is any value already at or above the floor. The collaborators
    are injectable so the policy is testable without a real process tree.
    """
    writes: list[tuple[int, int, int]] = []
    for pid in list_descendants(root_pid):
        current = read_adj(pid)
        if current is None or current >= bands.SHARED_BROWSER_FLOOR:
            continue
        remapped = bands.shared_browser_oom_score_adj(current)
        if write_adj(pid, remapped):
            writes.append((pid, current, remapped))
    return writes


class RetagScheduler:
    """Sweeps the process tree in a short burst after each ``kick()``.

    Idle (no burst pending), the worker thread blocks on the wake event and
    costs nothing. ``kick()`` opens (or extends) the burst window and wakes the
    thread, which then sweeps immediately and re-sweeps each interval until the
    window closes. All methods are thread-safe; ``kick()`` never blocks, so it
    is safe to call from the asyncio loop's event handlers.

    The sweep and the timing knobs are injectable so tests can drive the
    scheduler with a recorded sweep and tight timings.
    """

    def __init__(
        self,
        sweep: Callable[[], object] | None = None,
        burst_interval: float = _BURST_SWEEP_INTERVAL_SECONDS,
        burst_duration: float = _BURST_DURATION_SECONDS,
    ) -> None:
        self._sweep = sweep if sweep is not None else self._default_sweep
        self._burst_interval = burst_interval
        self._burst_duration = burst_duration
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._burst_deadline = 0.0
        self._thread: threading.Thread | None = None

    @staticmethod
    def _default_sweep() -> None:
        writes = sweep_once(os.getpid())
        if writes:
            logger.debug(
                "Raised Chromium processes into the browser shedding band: {}", writes
            )

    def start(self) -> None:
        """Start the worker thread (idempotent)."""
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._loop, name="oom-retag", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        """Stop the worker thread (for tests; the daemon flag already ties the
        thread's life to the process)."""
        self._stop.set()
        self._wake.set()

    def kick(self) -> None:
        """Note that new Chromium processes are expected: open (or extend) the
        burst window and wake the sweeper. A no-op scheduler-side if the thread
        was never started (e.g. under tests), beyond recording the deadline."""
        with self._lock:
            self._burst_deadline = time.monotonic() + self._burst_duration
        self._wake.set()

    def _remaining_burst(self) -> float:
        with self._lock:
            return self._burst_deadline - time.monotonic()

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self._remaining_burst() <= 0:
                # Idle: block until the next kick (or stop). The clear-then-
                # recheck order can at worst produce one spurious loop pass,
                # never a lost wakeup: kick() records the deadline before
                # setting the event.
                self._wake.wait()
                self._wake.clear()
                continue
            self._sweep()
            self._wake.wait(timeout=self._burst_interval)
            self._wake.clear()


_scheduler = RetagScheduler()


def start_oom_retagging() -> None:
    """Start the process-wide retagging worker (the service entry point calls
    this once; tests never do, so importing this module starts nothing)."""
    _scheduler.start()


def notify_chromium_processes_expected() -> None:
    """Report a fleet event that can spawn Chromium processes (launch, new
    page, navigation). Cheap and non-blocking; harmless when the worker was
    never started."""
    _scheduler.kick()
