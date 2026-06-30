"""Filesystem locations and timezone resolution for the scheduler.

All paths are relative to the repo root (the scheduler runs from /mngr/code via
supervisord). The schedule, state, and logs live under runtime/ so they are
shared by every agent on the host and ride the per-agent backup.
"""

from datetime import datetime, tzinfo
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from loguru import logger

SCHEDULE_PATH: Final[Path] = Path("runtime/scheduled_tasks.toml")
STATE_PATH: Final[Path] = Path("runtime/scheduler/state.toml")
TIMEZONE_PATH: Final[Path] = Path("runtime/scheduler/timezone")
LOG_DIR: Final[Path] = Path("runtime/scheduler/logs")


def resolve_timezone(timezone_path: Path = TIMEZONE_PATH) -> tzinfo:
    """Return the timezone schedules are interpreted in.

    Reads the IANA name written by the desktop client to
    runtime/scheduler/timezone (e.g. ``America/New_York``). Falls back to the
    host's local timezone if the file is absent, empty, or names an unknown zone.
    """
    if timezone_path.exists():
        name = timezone_path.read_text().strip()
        if name:
            try:
                return ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError):
                logger.warning(
                    "Invalid timezone {!r} in {}; falling back to the host clock",
                    name,
                    timezone_path,
                )
    local_tz = datetime.now().astimezone().tzinfo
    assert local_tz is not None, "astimezone() always yields an aware datetime"
    return local_tz
