"""Read and write runtime/scheduler/state.toml (per-task last-run state).

Each task gets a TOML table keyed by its name. ``last_run_at`` is stored as an
ISO-8601 string. Writes are atomic (temp file + rename) so a crash mid-write
never leaves a truncated state file.
"""

import os
import tomllib
from datetime import datetime
from pathlib import Path

import tomlkit

from scheduler.config import STATE_PATH
from scheduler.data_types import TaskRunState
from scheduler.errors import StateFileError


def load_state(path: Path = STATE_PATH) -> dict[str, TaskRunState]:
    """Load per-task run state. A missing file yields an empty mapping."""
    if not path.exists():
        return {}
    try:
        raw = tomllib.loads(path.read_text())
    except (tomllib.TOMLDecodeError, OSError) as error:
        raise StateFileError(f"Could not read {path}: {error}") from error

    state: dict[str, TaskRunState] = {}
    for name, entry in raw.items():
        last_run_raw = entry.get("last_run_at")
        last_run_at = datetime.fromisoformat(last_run_raw) if last_run_raw else None
        state[name] = TaskRunState(
            name=name,
            last_run_at=last_run_at,
            last_exit_code=entry.get("last_exit_code"),
            last_status=entry.get("last_status", "armed"),
        )
    return state


def save_state(state: dict[str, TaskRunState], path: Path = STATE_PATH) -> None:
    """Atomically write per-task run state."""
    document = tomlkit.document()
    for name, run_state in state.items():
        table = tomlkit.table()
        if run_state.last_run_at is not None:
            table["last_run_at"] = run_state.last_run_at.isoformat()
        if run_state.last_exit_code is not None:
            table["last_exit_code"] = run_state.last_exit_code
        table["last_status"] = run_state.last_status
        document[name] = table

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(tomlkit.dumps(document))
    os.replace(temp_path, path)
