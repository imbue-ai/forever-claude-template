"""Unit tests for per-task run-state persistence."""

from datetime import datetime, timezone
from pathlib import Path

from scheduler.data_types import TaskRunState
from scheduler.state import load_state, save_state

_UTC = timezone.utc


def test_missing_file_yields_empty_state(tmp_path: Path) -> None:
    assert load_state(tmp_path / "absent.toml") == {}


def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "scheduler" / "state.toml"
    state = {
        "caretaker": TaskRunState(
            name="caretaker",
            last_run_at=datetime(2026, 6, 25, 3, 0, tzinfo=_UTC),
            last_exit_code=0,
            last_status="ok",
        ),
        "armed": TaskRunState(
            name="armed", last_run_at=datetime(2026, 6, 25, 14, 0, tzinfo=_UTC)
        ),
    }
    save_state(state, path)
    loaded = load_state(path)
    assert loaded["caretaker"].last_run_at == datetime(2026, 6, 25, 3, 0, tzinfo=_UTC)
    assert loaded["caretaker"].last_exit_code == 0
    assert loaded["caretaker"].last_status == "ok"
    assert loaded["armed"].last_status == "armed"
    assert loaded["armed"].last_exit_code is None


def test_save_creates_parent_directory_and_file(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "scheduler" / "state.toml"
    save_state(
        {"x": TaskRunState(name="x", last_run_at=datetime(2026, 1, 1, tzinfo=_UTC))},
        path,
    )
    assert path.exists()


def test_save_overwrites_atomically(tmp_path: Path) -> None:
    path = tmp_path / "state.toml"
    save_state(
        {"x": TaskRunState(name="x", last_run_at=datetime(2026, 1, 1, tzinfo=_UTC))},
        path,
    )
    save_state(
        {"y": TaskRunState(name="y", last_run_at=datetime(2026, 2, 2, tzinfo=_UTC))},
        path,
    )
    loaded = load_state(path)
    assert set(loaded) == {"y"}
    assert not path.with_suffix(path.suffix + ".tmp").exists()
