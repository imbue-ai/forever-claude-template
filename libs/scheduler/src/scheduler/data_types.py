"""Data types for the file-driven scheduler.

A ``ScheduledTask`` is one ``[[task]]`` row of runtime/scheduled_tasks.toml.
Its ``TaskRunState`` (last fire time + last result) is tracked separately in
runtime/scheduler/state.toml so that catch-up survives reboots.
"""

from datetime import datetime
from typing import Literal

from imbue.imbue_common.frozen_model import FrozenModel
from pydantic import Field

# "armed" means the task has been seen but never actually run (its last_run_at is
# the moment it was first observed, set so that adding a task never triggers an
# immediate run -- it first fires at its next scheduled time).
TaskStatus = Literal["armed", "running", "ok", "error"]


class ScheduledTask(FrozenModel):
    """One scheduled task, as declared in runtime/scheduled_tasks.toml."""

    name: str = Field(description="Unique task identifier.")
    schedule: str = Field(
        description="Standard 5-field cron expression (croniter syntax)."
    )
    command: str = Field(description="Shell command to run from the repo root.")
    enabled: bool = Field(default=True, description="Whether the task is active.")
    catch_up: bool = Field(
        default=True,
        description="If true, run once on boot when fire times were missed during downtime.",
    )
    description: str = Field(
        default="", description="Human-readable note about the task."
    )


class TaskRunState(FrozenModel):
    """The recorded outcome of a task's most recent run (or arming)."""

    name: str = Field(description="The task this state belongs to.")
    last_run_at: datetime | None = Field(
        default=None,
        description="When the task last ran or was armed (timezone-aware).",
    )
    last_exit_code: int | None = Field(
        default=None, description="Exit code of the last completed run."
    )
    last_status: TaskStatus = Field(
        default="armed", description="Outcome of the last run."
    )
