"""Unit tests for the pure timing core."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from scheduler.data_types import ScheduledTask, TaskRunState
from scheduler.engine import (
    arm_new_tasks,
    is_task_due,
    most_recent_fire,
    select_due_tasks,
)

_UTC = timezone.utc
_TICK = 60


def _task(
    name: str = "t", schedule: str = "0 3 * * *", **kwargs: object
) -> ScheduledTask:
    return ScheduledTask(name=name, schedule=schedule, command="true", **kwargs)  # type: ignore[arg-type]


def test_most_recent_fire_is_inclusive_of_current_minute() -> None:
    now = datetime(2026, 6, 25, 3, 0, tzinfo=_UTC)
    fire = most_recent_fire("0 3 * * *", now, _UTC)
    assert fire == datetime(2026, 6, 25, 3, 0, tzinfo=_UTC)


def test_most_recent_fire_returns_prior_day_before_fire() -> None:
    now = datetime(2026, 6, 25, 2, 59, tzinfo=_UTC)
    fire = most_recent_fire("0 3 * * *", now, _UTC)
    assert fire == datetime(2026, 6, 24, 3, 0, tzinfo=_UTC)


def test_armed_task_is_not_due() -> None:
    # A newly seen task is armed (last_run_at = now) and must not run immediately.
    task = _task()
    now = datetime(2026, 6, 25, 14, 0, tzinfo=_UTC)
    state = arm_new_tasks([task], {}, now)
    assert state[task.name].last_run_at == now
    assert state[task.name].last_status == "armed"
    assert select_due_tasks([task], state, now, _UTC, _TICK) == []


def test_task_with_no_state_is_not_due() -> None:
    task = _task()
    now = datetime(2026, 6, 25, 4, 0, tzinfo=_UTC)
    assert is_task_due(task, None, now, _UTC, _TICK) is False


def test_task_is_due_when_fire_is_after_last_run() -> None:
    task = _task()
    last_run = datetime(2026, 6, 24, 3, 0, tzinfo=_UTC)
    now = datetime(2026, 6, 25, 3, 0, tzinfo=_UTC)
    assert is_task_due(task, last_run, now, _UTC, _TICK) is True


def test_task_not_due_when_last_run_after_fire() -> None:
    task = _task()
    last_run = datetime(2026, 6, 25, 3, 0, tzinfo=_UTC)
    now = datetime(2026, 6, 25, 12, 0, tzinfo=_UTC)
    assert is_task_due(task, last_run, now, _UTC, _TICK) is False


def test_missed_intervals_coalesce_into_one_run() -> None:
    # Hourly task, last ran 3 hours ago, machine just came back: due exactly once.
    task = _task(schedule="0 * * * *")
    last_run = datetime(2026, 6, 25, 9, 0, tzinfo=_UTC)
    now = datetime(2026, 6, 25, 12, 5, tzinfo=_UTC)
    state = {
        task.name: TaskRunState(name=task.name, last_run_at=last_run, last_status="ok")
    }
    assert select_due_tasks([task], state, now, _UTC, _TICK) == [task]
    # After the run stamps last_run_at = now, it is no longer due this minute.
    state_after = {
        task.name: TaskRunState(name=task.name, last_run_at=now, last_status="ok")
    }
    assert select_due_tasks([task], state_after, now, _UTC, _TICK) == []


def test_no_catch_up_skips_stale_miss() -> None:
    task = _task(schedule="0 * * * *", catch_up=False)
    last_run = datetime(2026, 6, 25, 9, 0, tzinfo=_UTC)
    # Fire was at 12:00 but it is now 12:30 -- a stale miss for a no-catch-up task.
    now = datetime(2026, 6, 25, 12, 30, tzinfo=_UTC)
    assert is_task_due(task, last_run, now, _UTC, _TICK) is False


def test_no_catch_up_runs_current_fire() -> None:
    task = _task(schedule="0 * * * *", catch_up=False)
    last_run = datetime(2026, 6, 25, 11, 0, tzinfo=_UTC)
    # Fire at 12:00, now 12:00:30 -- within the tick window, so it runs.
    now = datetime(2026, 6, 25, 12, 0, 30, tzinfo=_UTC)
    assert is_task_due(task, last_run, now, _UTC, _TICK) is True


def test_disabled_task_is_never_due() -> None:
    task = _task(enabled=False)
    last_run = datetime(2026, 6, 24, 3, 0, tzinfo=_UTC)
    now = datetime(2026, 6, 25, 3, 0, tzinfo=_UTC)
    assert is_task_due(task, last_run, now, _UTC, _TICK) is False


def test_schedule_is_interpreted_in_the_given_timezone() -> None:
    # "0 3 * * *" in America/New_York (EDT = UTC-4 on this date). The most recent
    # NY 3 AM fire as of 03:30 UTC is the *previous* day's 3 AM (07:00 UTC), which
    # is before last_run -- so it must NOT be due. (Under UTC it would be due.)
    new_york = ZoneInfo("America/New_York")
    task = _task(schedule="0 3 * * *")
    last_run = datetime(2026, 6, 24, 12, 0, tzinfo=_UTC)
    before_ny_3am = datetime(2026, 6, 25, 3, 30, tzinfo=_UTC)
    assert is_task_due(task, last_run, before_ny_3am, new_york, _TICK) is False
    # Once it is past 3 AM in New York (07:00 UTC), the fire is after last_run.
    after_ny_3am = datetime(2026, 6, 25, 7, 30, tzinfo=_UTC)
    assert is_task_due(task, last_run, after_ny_3am, new_york, _TICK) is True


def test_arm_new_tasks_leaves_existing_state_untouched() -> None:
    task = _task()
    existing = TaskRunState(
        name=task.name, last_run_at=datetime(2026, 6, 1, tzinfo=_UTC), last_status="ok"
    )
    now = datetime(2026, 6, 25, 14, 0, tzinfo=_UTC)
    result = arm_new_tasks([task], {task.name: existing}, now)
    assert result[task.name] is existing
