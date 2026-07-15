"""Keep the browser process tree in the shared-browser memory-shedding band.

The browser daemon is tagged to the top band (``SHARED_BROWSER``) at spawn and
everything it launches inherits that -- except Chromium, which deliberately
overwrites the inherited ``oom_score_adj`` once per process at startup with its
own internal gradation (browser/zygote 0, gpu/utility 200, renderers 300). Left
alone, that would make the memory-heavy renderers *more* protected than the
agents whose work they serve, inverting the shedding order.

The kernel cannot forbid that lowering without ``CAP_SYS_RESOURCE``, but
Chromium writes each value exactly once (its periodic re-adjustment is
ChromeOS-only), so an external raise sticks. This module runs a small periodic
sweep over the daemon's descendants that remaps every value found below
``SHARED_BROWSER_FLOOR`` into the band's range, preserving Chromium's relative
ordering in compressed form (the gradation is worth keeping: shedding one
renderer kills one tab, not the whole browser). The sweep only ever raises,
never lowers, and never touches a value already at or above the floor -- so the
node/Playwright driver (inherited ceiling), crashpad (inherited ceiling), and
already-remapped processes are left alone, and repeated sweeps are idempotent.

Runs on a plain daemon thread rather than the bridge's asyncio loop: it touches
no session state (only ``/proc``), and must keep working even when the loop is
busy driving browsers -- which is exactly when memory pressure peaks.
"""

import os
import threading
from collections.abc import Callable

from loguru import logger
from oom_priority import bands
from oom_priority.proctree import list_descendant_pids

# How often to re-check the tree. New Chromium processes (a renderer per new
# tab) appear at Chrome's self-assigned value until the next sweep, so the
# period bounds that exposure window; pressure builds over seconds, and the walk
# is a cheap read of a few dozen /proc files, so a short period is fine.
_SWEEP_SECONDS = 5.0


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


def _sweep_loop(stop: threading.Event) -> None:
    root_pid = os.getpid()
    while not stop.wait(_SWEEP_SECONDS):
        writes = sweep_once(root_pid)
        if writes:
            logger.debug(
                "Raised Chromium processes into the browser shedding band: {}", writes
            )


def start_retag_thread() -> threading.Event:
    """Start the periodic sweep on a daemon thread; return its stop event.

    The returned event is for tests and clean shutdown -- the daemon flag
    already ties the thread's life to the process.
    """
    stop = threading.Event()
    thread = threading.Thread(
        target=_sweep_loop, args=(stop,), name="oom-retag", daemon=True
    )
    thread.start()
    return stop
