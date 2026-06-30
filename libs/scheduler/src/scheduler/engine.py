"""Pure timing core: decide which tasks are due, given the current time + state.

These functions perform no I/O and read no clock of their own -- callers pass
``now`` (timezone-aware) and the resolved schedule timezone, so the logic is
fully deterministic and unit-testable.
"""

from datetime import datetime, timedelta, tzinfo

from croniter import croniter

from scheduler.data_types import ScheduledTask, TaskRunState


def most_recent_fire(schedule: str, now: datetime, tz: tzinfo) -> datetime | None:
    """Return the latest scheduled fire time at or before ``now``.

    The schedule is interpreted in ``tz``. The returned datetime is timezone-aware
    (in ``tz``), truncated to the minute. Returns ``None`` only if the cron
    expression yields no prior fire (not expected for valid 5-field crons).
    """
    now_local = now.astimezone(tz)
    # croniter.get_prev returns the fire strictly before its anchor; anchoring one
    # minute past the current minute makes the result inclusive of the current minute.
    anchor = now_local.replace(second=0, microsecond=0) + timedelta(minutes=1)
    iterator = croniter(schedule, anchor)
    fire = iterator.get_prev(datetime)
    return fire


def is_task_due(
    task: ScheduledTask,
    last_run_at: datetime | None,
    now: datetime,
    tz: tzinfo,
    tick_seconds: int,
) -> bool:
    """Return True if ``task`` should run on this tick.

    A task is due when its most recent fire time is strictly after the last time
    it ran. Considering only the single most recent fire time means several
    intervals missed during downtime coalesce into one run. A disabled task, or a
    task that has never been armed (``last_run_at is None``), is never due. When
    ``catch_up`` is false, a fire older than the current tick is treated as a
    stale miss and skipped.
    """
    if not task.enabled:
        return False
    fire = most_recent_fire(task.schedule, now, tz)
    if fire is None:
        return False
    if last_run_at is None:
        return False
    if fire <= last_run_at:
        return False
    if task.catch_up:
        return True
    return (now - fire) <= timedelta(seconds=tick_seconds)


def select_due_tasks(
    tasks: list[ScheduledTask],
    state: dict[str, TaskRunState],
    now: datetime,
    tz: tzinfo,
    tick_seconds: int,
) -> list[ScheduledTask]:
    """Return the subset of ``tasks`` that are due to run on this tick."""
    due: list[ScheduledTask] = []
    for task in tasks:
        run_state = state.get(task.name)
        last_run_at = run_state.last_run_at if run_state is not None else None
        if is_task_due(task, last_run_at, now, tz, tick_seconds):
            due.append(task)
    return due


def arm_new_tasks(
    tasks: list[ScheduledTask],
    state: dict[str, TaskRunState],
    now: datetime,
) -> dict[str, TaskRunState]:
    """Return ``state`` with every not-yet-seen task armed at ``now``.

    Arming records ``last_run_at = now`` without running, so a freshly added or
    seeded task never fires immediately; it first runs at its next scheduled time.
    Existing state entries are left untouched.
    """
    updated = dict(state)
    for task in tasks:
        if task.name not in updated:
            updated[task.name] = TaskRunState(
                name=task.name, last_run_at=now, last_status="armed"
            )
    return updated
