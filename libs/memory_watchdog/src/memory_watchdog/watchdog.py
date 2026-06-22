"""Memory watchdog service.

Each poll it snapshots the container's process tree, classifies every process
into an OOM-priority tier, and keeps each process's oom_score_adj in line with
its tier. Under sustained memory pressure it sheds whole tiers from the most
expendable up (agent build/test/browser subprocesses first, the user's own
agents only as a last resort), recording every kill to a ledger and publishing a
status file the UI banner reads.

Liveness of the watchdog itself, and of every other background service, is owned
by supervisord (see supervisord.conf): supervisord restarts this process if it
dies, and restarts any service it sheds. The watchdog does not supervise other
processes -- it only decides which to shed under pressure.
"""

import os
import signal
import subprocess
import threading
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Final

from imbue.imbue_common.logging import format_nanosecond_iso_timestamp
from imbue.imbue_common.pure import pure
from loguru import logger

from memory_watchdog.classifier import classify_processes, find_services_session_name
from memory_watchdog.data_types import (
    ISO_TIMESTAMP_FORMAT,
    SHEDDABLE_TIERS_IN_SHED_ORDER,
    MemoryPressure,
    MemoryStatus,
    ProcessClassification,
    ShedRecord,
)
from memory_watchdog.ledger import (
    append_shed_records,
    read_currently_blocked_services,
    write_status,
)
from memory_watchdog.shedder import shed_tier, summarize_recent_sheds
from memory_watchdog.system_probe import (
    read_agent_label_sets,
    read_all_processes,
    read_memory_pressure,
    read_tmux_panes,
)
from memory_watchdog.tagger import apply_oom_score_adjustments

POLL_INTERVAL_SECONDS: Final[float] = 3.0
# Used-fraction at which the shedder arms, and how long it must stay there before
# the first kill. Hysteresis: pressure must be sustained, not a momentary spike.
SHED_PRESSURE_THRESHOLD: Final[float] = 0.90
SHED_SUSTAINED_SECONDS: Final[float] = 10.0
# The shedder stops escalating once usage drops back below this (lower than the
# arm threshold so it does not immediately re-arm on noise).
SHED_RELIEF_THRESHOLD: Final[float] = 0.80
# The banner shows while usage is above this, or while anything was shed within
# the recent window.
BANNER_PRESSURE_THRESHOLD: Final[float] = 0.80
RECENT_SHED_WINDOW_SECONDS: Final[float] = 120.0

_MNGR_PREFIX_DEFAULT: Final[str] = "mngr-"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return format_nanosecond_iso_timestamp(moment)


def _tmux_current_session_name() -> str:
    """Best-effort tmux "current session" fallback used until supervisord is up.

    Only consulted on the first polls before supervisord exists in the process
    snapshot; once it does, the services session is derived from supervisord's
    pane ancestor (see find_services_session_name), which is unambiguous.
    """
    result = subprocess.run(
        ["tmux", "display-message", "-p", "#S"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


@pure
def _build_status(
    pressure: MemoryPressure,
    recent_records: Sequence[ShedRecord],
    blocked_services: Sequence[str],
    now_iso: str,
) -> MemoryStatus:
    is_under_pressure = (
        pressure.used_fraction >= BANNER_PRESSURE_THRESHOLD or len(recent_records) > 0
    )
    return MemoryStatus(
        timestamp=now_iso,
        used_fraction=pressure.used_fraction,
        total_kb=pressure.total_kb,
        available_kb=pressure.available_kb,
        pressure_threshold_fraction=SHED_PRESSURE_THRESHOLD,
        is_under_pressure=is_under_pressure,
        recently_shed=tuple(summarize_recent_sheds(recent_records)),
        blocked_services=tuple(blocked_services),
    )


def _shed_until_relieved(
    classifications: Sequence[ProcessClassification],
) -> list[ShedRecord]:
    """Shed tiers from most expendable up, re-reading pressure between tiers.

    Stops as soon as usage drops below the relief threshold, so the user's own
    agents (the last sheddable tier) are only killed when nothing cheaper frees
    enough memory.
    """
    all_records: list[ShedRecord] = []
    for tier in SHEDDABLE_TIERS_IN_SHED_ORDER:
        records = shed_tier(classifications, tier)
        if records:
            logger.warning(
                "Shed {} process(es) from tier {} to relieve memory pressure",
                len(records),
                tier,
            )
            append_shed_records(records)
            all_records.extend(records)
        pressure_after = read_memory_pressure()
        if pressure_after.used_fraction < SHED_RELIEF_THRESHOLD:
            break
    return all_records


def _prune_recent_records(
    recent_records: Sequence[ShedRecord], now: datetime
) -> list[ShedRecord]:
    """Drop shed records older than the recent window (used for the banner)."""
    cutoff = now.timestamp() - RECENT_SHED_WINDOW_SECONDS
    kept: list[ShedRecord] = []
    for record in recent_records:
        try:
            record_time = datetime.strptime(
                record.timestamp, ISO_TIMESTAMP_FORMAT
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if record_time.timestamp() >= cutoff:
            kept.append(record)
    return kept


def main() -> None:
    mngr_prefix = os.environ.get("MNGR_PREFIX", _MNGR_PREFIX_DEFAULT)
    logger.info("Started memory watchdog (agent prefix: {})", mngr_prefix)

    stop_event = threading.Event()

    def _handle_signal(signum: int, frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Loop state.
    pressure_over_threshold_since: float | None = None
    recent_records: list[ShedRecord] = []

    while not stop_event.is_set():
        now = _now()

        # Snapshot the system and classify every process.
        processes = read_all_processes()
        panes = read_tmux_panes()
        user_created_names, agent_created_names = read_agent_label_sets()
        # The watchdog has no tmux pane of its own (it is a supervisord child),
        # so derive the services session from supervisord's pane ancestor. Fall
        # back to tmux's notion of the current session only on the first polls,
        # before supervisord appears in the snapshot.
        services_session_name = (
            find_services_session_name(processes, panes) or _tmux_current_session_name()
        )
        classifications = classify_processes(
            processes=processes,
            panes=panes,
            services_session_name=services_session_name,
            mngr_prefix=mngr_prefix,
            user_created_agent_names=user_created_names,
            agent_created_agent_names=agent_created_names,
        )

        # Keep oom_score_adj in sync (the kernel-level last resort under runc).
        apply_oom_score_adjustments(classifications)

        # Decide whether sustained pressure warrants shedding.
        pressure = read_memory_pressure()
        if pressure.used_fraction >= SHED_PRESSURE_THRESHOLD:
            if pressure_over_threshold_since is None:
                pressure_over_threshold_since = now.timestamp()
            sustained_for = now.timestamp() - pressure_over_threshold_since
            if sustained_for >= SHED_SUSTAINED_SECONDS:
                new_records = _shed_until_relieved(classifications)
                recent_records.extend(new_records)
                pressure_over_threshold_since = None
        else:
            pressure_over_threshold_since = None

        # Publish the status the UI banner reads.
        recent_records = _prune_recent_records(recent_records, now)
        blocked_services = read_currently_blocked_services()
        write_status(
            _build_status(pressure, recent_records, blocked_services, _iso(now))
        )

        stop_event.wait(POLL_INTERVAL_SECONDS)

    logger.info("Memory watchdog stopped")


if __name__ == "__main__":
    main()
