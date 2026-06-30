"""Unit tests for reading and editing the schedule file."""

from pathlib import Path

import pytest

from scheduler.data_types import ScheduledTask
from scheduler.errors import ScheduleFileError
from scheduler.schedule_file import add_task, read_schedule, remove_task

_SAMPLE = """
[[task]]
name = "caretaker"
schedule = "0 3 * * *"
command = "bash run.sh"
enabled = true
catch_up = true
description = "Nightly run."

[[task]]
name = "hourly"
schedule = "0 * * * *"
command = "echo hi"
enabled = false
catch_up = false
description = ""
"""


def test_missing_file_yields_no_tasks(tmp_path: Path) -> None:
    assert read_schedule(tmp_path / "absent.toml") == []


def test_reads_and_validates_tasks(tmp_path: Path) -> None:
    path = tmp_path / "scheduled_tasks.toml"
    path.write_text(_SAMPLE)
    tasks = read_schedule(path)
    assert [task.name for task in tasks] == ["caretaker", "hourly"]
    assert tasks[0].catch_up is True
    assert tasks[1].enabled is False


def test_invalid_cron_raises(tmp_path: Path) -> None:
    path = tmp_path / "scheduled_tasks.toml"
    path.write_text(
        '[[task]]\nname = "bad"\nschedule = "not a cron"\ncommand = "true"\n'
    )
    with pytest.raises(ScheduleFileError):
        read_schedule(path)


def test_duplicate_name_raises(tmp_path: Path) -> None:
    path = tmp_path / "scheduled_tasks.toml"
    path.write_text(
        '[[task]]\nname = "x"\nschedule = "0 3 * * *"\ncommand = "a"\n'
        '[[task]]\nname = "x"\nschedule = "0 4 * * *"\ncommand = "b"\n'
    )
    with pytest.raises(ScheduleFileError):
        read_schedule(path)


def test_add_then_read_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "scheduled_tasks.toml"
    add_task(
        ScheduledTask(name="backup", schedule="0 * * * *", command="do-backup"), path
    )
    tasks = read_schedule(path)
    assert len(tasks) == 1
    assert tasks[0].name == "backup"
    assert tasks[0].command == "do-backup"


def test_add_preserves_existing_tasks(tmp_path: Path) -> None:
    path = tmp_path / "scheduled_tasks.toml"
    path.write_text(_SAMPLE)
    add_task(ScheduledTask(name="new", schedule="*/5 * * * *", command="tick"), path)
    names = [task.name for task in read_schedule(path)]
    assert names == ["caretaker", "hourly", "new"]


def test_add_duplicate_raises_without_replace(tmp_path: Path) -> None:
    path = tmp_path / "scheduled_tasks.toml"
    path.write_text(_SAMPLE)
    with pytest.raises(ScheduleFileError):
        add_task(
            ScheduledTask(name="caretaker", schedule="0 5 * * *", command="x"), path
        )


def test_add_with_replace_overwrites_in_place(tmp_path: Path) -> None:
    path = tmp_path / "scheduled_tasks.toml"
    path.write_text(_SAMPLE)
    add_task(
        ScheduledTask(name="caretaker", schedule="0 5 * * *", command="x"),
        path,
        replace=True,
    )
    tasks = read_schedule(path)
    assert [task.name for task in tasks] == ["caretaker", "hourly"]
    assert tasks[0].schedule == "0 5 * * *"


def test_add_invalid_cron_raises(tmp_path: Path) -> None:
    path = tmp_path / "scheduled_tasks.toml"
    with pytest.raises(ScheduleFileError):
        add_task(ScheduledTask(name="bad", schedule="nope", command="x"), path)


def test_remove_existing_and_missing(tmp_path: Path) -> None:
    path = tmp_path / "scheduled_tasks.toml"
    path.write_text(_SAMPLE)
    assert remove_task("hourly", path) is True
    assert [task.name for task in read_schedule(path)] == ["caretaker"]
    assert remove_task("not-there", path) is False


def test_remove_from_missing_file_is_false(tmp_path: Path) -> None:
    assert remove_task("x", tmp_path / "absent.toml") is False
