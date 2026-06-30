"""Read and edit runtime/scheduled_tasks.toml.

Reads go through stdlib ``tomllib``; edits use ``tomlkit`` so comments and
formatting survive both agent edits and programmatic ones.
"""

import tomllib
from pathlib import Path

import tomlkit
from croniter import croniter
from pydantic import ValidationError
from tomlkit.items import Table

from scheduler.config import SCHEDULE_PATH
from scheduler.data_types import ScheduledTask
from scheduler.errors import ScheduleFileError


def read_schedule(path: Path = SCHEDULE_PATH) -> list[ScheduledTask]:
    """Parse and validate the schedule file. A missing file yields no tasks."""
    if not path.exists():
        return []
    try:
        raw = tomllib.loads(path.read_text())
    except (tomllib.TOMLDecodeError, OSError) as error:
        raise ScheduleFileError(f"Could not read {path}: {error}") from error

    tasks: list[ScheduledTask] = []
    for entry in raw.get("task", []):
        try:
            tasks.append(ScheduledTask.model_validate(entry))
        except ValidationError as error:
            raise ScheduleFileError(f"Invalid task entry in {path}: {error}") from error

    _validate_tasks(tasks, path)
    return tasks


def _validate_tasks(tasks: list[ScheduledTask], path: Path) -> None:
    seen: set[str] = set()
    for task in tasks:
        if task.name in seen:
            raise ScheduleFileError(f"Duplicate task name {task.name!r} in {path}")
        seen.add(task.name)
        if not croniter.is_valid(task.schedule):
            raise ScheduleFileError(
                f"Invalid cron expression {task.schedule!r} for task {task.name!r} in {path}"
            )


def add_task(
    task: ScheduledTask, path: Path = SCHEDULE_PATH, *, replace: bool = False
) -> None:
    """Append ``task`` to the schedule file (creating it if needed).

    Raises if a task with the same name already exists unless ``replace`` is set,
    in which case the existing entry is overwritten in place.
    """
    if not croniter.is_valid(task.schedule):
        raise ScheduleFileError(
            f"Invalid cron expression {task.schedule!r} for task {task.name!r}"
        )

    document = tomlkit.parse(path.read_text()) if path.exists() else tomlkit.document()
    table_array = document.get("task")
    if table_array is None:
        table_array = tomlkit.aot()
        document["task"] = table_array

    existing_index = _find_task_index(table_array, task.name)
    if existing_index is not None and not replace:
        raise ScheduleFileError(f"Task {task.name!r} already exists in {path}")

    new_entry = _task_to_table(task)
    if existing_index is not None:
        table_array[existing_index] = new_entry
    else:
        table_array.append(new_entry)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(document))


def remove_task(name: str, path: Path = SCHEDULE_PATH) -> bool:
    """Remove the task named ``name``. Returns True if a task was removed."""
    if not path.exists():
        return False
    document = tomlkit.parse(path.read_text())
    table_array = document.get("task")
    if table_array is None:
        return False
    index = _find_task_index(table_array, name)
    if index is None:
        return False
    del table_array[index]
    path.write_text(tomlkit.dumps(document))
    return True


def _find_task_index(table_array: object, name: str) -> int | None:
    for index, entry in enumerate(table_array):  # type: ignore[arg-type]
        if entry.get("name") == name:
            return index
    return None


def _task_to_table(task: ScheduledTask) -> Table:
    table = tomlkit.table()
    table["name"] = task.name
    table["schedule"] = task.schedule
    table["command"] = task.command
    table["enabled"] = task.enabled
    table["catch_up"] = task.catch_up
    table["description"] = task.description
    return table
