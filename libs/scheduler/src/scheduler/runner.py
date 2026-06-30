"""The scheduler daemon: tick on an interval, launch due tasks, persist state.

Tasks are launched as background subprocesses so a long-running task (e.g. the
nightly Caretaker) never blocks the tick loop, and a task already running is
never launched again (the running guard). ``last_run_at`` is stamped at launch
time, so while a task runs its most recent fire is no longer after ``last_run_at``
and it is not re-launched.
"""

import subprocess
import time
from datetime import datetime, timezone
from io import TextIOWrapper
from pathlib import Path
from typing import Final

from loguru import logger

from scheduler.config import LOG_DIR, SCHEDULE_PATH, STATE_PATH, resolve_timezone
from scheduler.data_types import ScheduledTask, TaskRunState
from scheduler.engine import arm_new_tasks, select_due_tasks
from scheduler.errors import SchedulerError
from scheduler.schedule_file import read_schedule
from scheduler.state import load_state, save_state

TICK_SECONDS: Final[int] = 60


class _RunningTask:
    """A launched task subprocess and the log file it writes to."""

    def __init__(
        self, process: subprocess.Popen[bytes], log_file: TextIOWrapper
    ) -> None:
        self.process = process
        self.log_file = log_file


def run_loop(
    schedule_path: Path = SCHEDULE_PATH,
    state_path: Path = STATE_PATH,
    log_dir: Path = LOG_DIR,
    tick_seconds: int = TICK_SECONDS,
    run_once: bool = False,
) -> None:
    """Run the scheduler tick loop forever (or a single tick if ``run_once``).

    Runs an immediate tick on startup so any task missed during downtime is
    caught up without waiting a full interval.
    """
    running: dict[str, _RunningTask] = {}
    logger.info(
        "Scheduler starting (tick={}s); schedule={}", tick_seconds, schedule_path
    )
    while True:
        try:
            _tick(schedule_path, state_path, log_dir, tick_seconds, running)
        except SchedulerError as error:
            logger.error("Skipping tick due to scheduler error: {}", error)
        if run_once:
            return
        time.sleep(tick_seconds)


def _tick(
    schedule_path: Path,
    state_path: Path,
    log_dir: Path,
    tick_seconds: int,
    running: dict[str, _RunningTask],
) -> None:
    now = datetime.now(timezone.utc)
    tz = resolve_timezone()
    tasks = read_schedule(schedule_path)

    state = load_state(state_path)
    _reap_finished(running, state, now)
    state = arm_new_tasks(tasks, state, now)

    due = select_due_tasks(tasks, state, now, tz, tick_seconds)
    for task in due:
        if task.name in running:
            logger.info(
                "Task {!r} is still running from a previous tick; skipping", task.name
            )
            continue
        _launch(task, log_dir, running, state, now)

    save_state(state, state_path)


def _reap_finished(
    running: dict[str, _RunningTask],
    state: dict[str, TaskRunState],
    now: datetime,
) -> None:
    for name in list(running.keys()):
        entry = running[name]
        exit_code = entry.process.poll()
        if exit_code is None:
            continue
        entry.log_file.close()
        status = "ok" if exit_code == 0 else "error"
        previous = state.get(name)
        last_run_at = previous.last_run_at if previous is not None else now
        state[name] = TaskRunState(
            name=name,
            last_run_at=last_run_at,
            last_exit_code=exit_code,
            last_status=status,
        )
        logger.info(
            "Task {!r} finished with exit code {} ({})", name, exit_code, status
        )
        del running[name]


def _launch(
    task: ScheduledTask,
    log_dir: Path,
    running: dict[str, _RunningTask],
    state: dict[str, TaskRunState],
    now: datetime,
) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{task.name}.log"
    log_file = log_path.open("a")
    log_file.write(f"\n===== {now.isoformat()} running: {task.command} =====\n")
    log_file.flush()
    process = subprocess.Popen(
        task.command, shell=True, stdout=log_file, stderr=subprocess.STDOUT
    )
    running[task.name] = _RunningTask(process, log_file)
    state[task.name] = TaskRunState(
        name=task.name, last_run_at=now, last_status="running"
    )
    logger.info("Launched task {!r}: {}", task.name, task.command)
