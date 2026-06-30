"""Exceptions raised by the scheduler."""


class SchedulerError(Exception):
    """Base class for all scheduler errors."""


class ScheduleFileError(SchedulerError):
    """Raised when runtime/scheduled_tasks.toml cannot be parsed or validated."""


class StateFileError(SchedulerError):
    """Raised when runtime/scheduler/state.toml cannot be parsed."""
