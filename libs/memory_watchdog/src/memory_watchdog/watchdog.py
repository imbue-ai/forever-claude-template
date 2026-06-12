"""Memory watchdog service.

Each poll it snapshots the container's process tree, classifies every process
into an OOM-priority tier, and keeps each process's oom_score_adj in line with
its tier. Under sustained memory pressure it sheds whole tiers from the most
expendable up (agent build/test/browser subprocesses first, the user's own
agents only as a last resort), recording every kill to a ledger and publishing a
status file the UI banner reads. It also supervises the service manager and the
telegram / terminal windows, restarting them if their process dies -- the
reverse of bootstrap restarting this watchdog, closing the recovery loop.
"""

import os
import signal
import subprocess
import threading
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Final

from imbue.imbue_common.pure import pure
from loguru import logger

from memory_watchdog.classifier import classify_processes
from memory_watchdog.data_types import (
    SHEDDABLE_TIERS_IN_SHED_ORDER,
    MemoryPressure,
    MemoryStatus,
    ProcessClassification,
    ProcessInfo,
    ShedRecord,
    TmuxPane,
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
# Supervised non-service windows and the command to relaunch each if its process
# dies. bootstrap is the critical one (nothing else restarts it); terminal and
# telegram are included so a memory kill does not leave them dead.
SUPERVISED_WINDOW_COMMANDS: Final[dict[str, str]] = {
    "bootstrap": "uv run bootstrap",
    "telegram": "uv run telegram-bot",
    "terminal": "bash scripts/run_ttyd.sh",
}
# A supervised window must look dead for this many consecutive polls before we
# relaunch it, and we will not relaunch the same window more often than the
# cooldown, so a service that exits cleanly is not thrashed.
SUPERVISION_DEAD_POLLS_BEFORE_RESTART: Final[int] = 2
SUPERVISION_RESTART_COOLDOWN_SECONDS: Final[float] = 30.0

_MNGR_PREFIX_DEFAULT: Final[str] = "mngr-"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f000Z")


def _get_services_session_name() -> str:
    result = subprocess.run(
        ["tmux", "display-message", "-p", "#S"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def _looks_like_idle_shell(command_line: str) -> bool:
    """Whether a process command looks like a bare interactive shell.

    Used to tell a window whose service has exited (leaving an idle shell at the
    pane) from one whose service `exec`'d into the pane itself (e.g. ttyd), which
    legitimately has no child process.
    """
    if not command_line:
        return True
    first_token = command_line.split(" ", 1)[0]
    basename = first_token.rsplit("/", 1)[-1].lstrip("-")
    return basename in {"bash", "sh", "zsh", "dash"}


@pure
def _windows_with_no_live_process(
    panes: Sequence[TmuxPane],
    processes: Sequence[ProcessInfo],
    services_session_name: str,
    supervised_window_names: frozenset[str],
) -> set[str]:
    """Return supervised windows that have died down to an idle shell.

    A running service is a direct child of its window's pane shell, so a window
    is considered dead only when its pane process is itself a bare shell with no
    children. Requiring the shell check avoids falsely flagging a window whose
    service `exec`'d into the pane (e.g. ttyd with no client attached has no
    child but is very much alive). Only windows in the services session are
    considered.
    """
    process_by_pid: dict[int, ProcessInfo] = {p.pid: p for p in processes}
    pids_with_parent: dict[int, list[int]] = defaultdict(list)
    for process in processes:
        pids_with_parent[process.parent_pid].append(process.pid)
    dead_windows: set[str] = set()
    for pane in panes:
        if pane.session_name != services_session_name:
            continue
        if pane.window_name not in supervised_window_names:
            continue
        pane_process = process_by_pid.get(pane.pane_pid)
        if pane_process is None:
            continue
        has_no_children = not pids_with_parent.get(pane.pane_pid)
        if has_no_children and _looks_like_idle_shell(pane_process.command_line):
            dead_windows.add(pane.window_name)
    return dead_windows


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


def _restart_window(session_name: str, window_name: str, command: str) -> None:
    """Relaunch a supervised window's command via tmux, creating it if missing."""
    window_target = f"{session_name}:{window_name}"
    logger.warning("Restarting supervised window {} ({})", window_name, command)
    list_result = subprocess.run(
        ["tmux", "list-windows", "-t", session_name, "-F", "#{window_name}"],
        capture_output=True,
        text=True,
        check=False,
    )
    existing_windows = (
        list_result.stdout.split("\n") if list_result.returncode == 0 else []
    )
    if window_name not in existing_windows:
        subprocess.run(
            ["tmux", "new-window", "-t", session_name, "-n", window_name, "-d"],
            check=False,
        )
    subprocess.run(
        ["tmux", "send-keys", "-t", window_target, command, "Enter"],
        check=False,
    )


def _prune_recent_records(
    recent_records: Sequence[ShedRecord], now: datetime
) -> list[ShedRecord]:
    """Drop shed records older than the recent window (used for the banner)."""
    cutoff = now.timestamp() - RECENT_SHED_WINDOW_SECONDS
    kept: list[ShedRecord] = []
    for record in recent_records:
        try:
            record_time = datetime.strptime(
                record.timestamp, "%Y-%m-%dT%H:%M:%S.%f000Z"
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if record_time.timestamp() >= cutoff:
            kept.append(record)
    return kept


def main() -> None:
    services_session_name = _get_services_session_name()
    mngr_prefix = os.environ.get("MNGR_PREFIX", _MNGR_PREFIX_DEFAULT)
    supervised_window_names = frozenset(SUPERVISED_WINDOW_COMMANDS)
    logger.info(
        "Started memory watchdog (services session: {}, agent prefix: {})",
        services_session_name,
        mngr_prefix,
    )

    stop_event = threading.Event()

    def _handle_signal(signum: int, frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Loop state.
    pressure_over_threshold_since: float | None = None
    recent_records: list[ShedRecord] = []
    dead_poll_count_by_window: dict[str, int] = defaultdict(int)
    last_restart_time_by_window: dict[str, float] = {}

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
                new_records = _shed_until_relieved(classifications)
                recent_records.extend(new_records)
                pressure_over_threshold_since = None
        else:
            pressure_over_threshold_since = None

        # Supervise the recovery / interface windows; restart any that died.
        dead_windows = _windows_with_no_live_process(
            panes, processes, services_session_name, supervised_window_names
        )
        for window_name in supervised_window_names:
            if window_name in dead_windows:
                dead_poll_count_by_window[window_name] = (
                    dead_poll_count_by_window[window_name] + 1
                )
            else:
                dead_poll_count_by_window[window_name] = 0
            is_dead_enough = (
                dead_poll_count_by_window[window_name]
                >= SUPERVISION_DEAD_POLLS_BEFORE_RESTART
            )
            last_restart = last_restart_time_by_window.get(window_name, 0.0)
            is_off_cooldown = (
                now.timestamp() - last_restart >= SUPERVISION_RESTART_COOLDOWN_SECONDS
            )
            if is_dead_enough and is_off_cooldown:
                _restart_window(
                    services_session_name,
                    window_name,
                    SUPERVISED_WINDOW_COMMANDS[window_name],
                )
                last_restart_time_by_window[window_name] = now.timestamp()
                dead_poll_count_by_window[window_name] = 0

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
