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
import threading
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Final

from imbue.imbue_common.logging import format_nanosecond_iso_timestamp
from imbue.imbue_common.pure import pure
from loguru import logger

from memory_watchdog.classifier import classify_processes
from memory_watchdog.data_types import (
    ISO_TIMESTAMP_FORMAT,
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
from memory_watchdog.shedder import (
    select_tiers_to_shed,
    shed_tier,
    summarize_recent_sheds,
)
from memory_watchdog.system_probe import (
    read_agent_label_sets,
    read_all_processes,
    read_memory_pressure,
    read_tmux_panes,
)
from memory_watchdog.tagger import apply_oom_score_adjustments

# How often we re-snapshot and re-tag. This is deliberately short, but NOT to
# "beat" the kernel OOM killer -- that fires synchronously the instant an
# allocation can't be satisfied, so nothing can preempt it. The interval instead
# governs how fresh each process's oom_score_adj is: under runc (lima),
# oom_score_adj is the real lever, so a process that spawns and grows between
# polls is untagged when the kernel picks a victim. A short interval keeps the
# tags current; a poll is cheap (one /proc walk, one tmux list-panes, a few small
# file reads), so the cost is negligible.
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


def _services_session_name(mngr_prefix: str) -> str:
    """The tmux session that runs supervisord and the background services.

    The watchdog runs as a supervisord child of the services agent (in a minds
    workspace, the constant-named ``system-services`` agent), so the services
    session is that agent's own tmux session, named ``<mngr_prefix><agent_name>``.
    The agent name is exported as MNGR_AGENT_NAME in the agent environment that
    supervisord -- and therefore this process -- inherits, so we read it directly
    rather than reconstructing it by walking the process tree.

    If MNGR_AGENT_NAME is somehow unset, the result matches no real session, so
    the services session's processes are simply classified like any other
    agent's (the pane shell stays infrastructure and the main process defaults to
    the protected user-agent tier -- never shed early). We log once so the
    misconfiguration is visible.
    """
    agent_name = os.environ.get("MNGR_AGENT_NAME", "")
    if not agent_name:
        logger.warning(
            "MNGR_AGENT_NAME is not set; cannot identify the services session by name. "
            "Its processes will be treated like any other agent's (still protected)."
        )
    return f"{mngr_prefix}{agent_name}"


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
    pressure: MemoryPressure,
) -> list[ShedRecord]:
    """Shed tiers from most expendable up, stopping once the projected reclaim
    is enough.

    Which tiers to shed is decided up front by ``select_tiers_to_shed``, which
    projects how much each tier would free from its processes' resident memory
    instead of re-reading /proc between kills. That avoids the over-shed bug
    where the kernel had not yet reclaimed a just-killed process's pages, so an
    immediate re-read still showed high usage and the shedder escalated into the
    user's own agents (the last sheddable tier) even though a cheaper tier had
    already freed enough. The next poll re-reads real usage and sheds again if
    the estimate fell short.
    """
    tiers_to_shed = select_tiers_to_shed(
        classifications,
        pressure.available_kb,
        pressure.total_kb,
        SHED_RELIEF_THRESHOLD,
    )
    all_records: list[ShedRecord] = []
    for tier in tiers_to_shed:
        records = shed_tier(classifications, tier)
        if records:
            logger.warning(
                "Shed {} process(es) from tier {} to relieve memory pressure",
                len(records),
                tier,
            )
            append_shed_records(records)
            all_records.extend(records)
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
    # The services session is fixed for the life of the process (it comes from
    # the inherited agent environment, which never changes), so resolve it once.
    services_session_name = _services_session_name(mngr_prefix)
    logger.info(
        "Started memory watchdog (agent prefix: {}, services session: {})",
        mngr_prefix,
        services_session_name,
    )

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
                new_records = _shed_until_relieved(classifications, pressure)
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
