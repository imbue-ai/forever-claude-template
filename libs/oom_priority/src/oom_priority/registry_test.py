import json
from pathlib import Path

import pytest

from oom_priority import registry


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OOM_PRIORITY_RUNTIME_DIR", str(tmp_path))
    return tmp_path


def test_record_and_lookup_round_trip(runtime: Path) -> None:
    registry.record_agent_pid(4242, "alpha", is_worker=True)
    found = registry.lookup_agent(4242)
    assert found is not None
    assert found["agent_name"] == "alpha"
    assert found["is_worker"] is True


def test_lookup_unknown_pid_returns_none(runtime: Path) -> None:
    assert registry.lookup_agent(4242) is None


def test_prune_removes_dead_pid_entries_and_keeps_live_ones(runtime: Path) -> None:
    directory = registry.agent_pids_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "100.json").write_text(json.dumps({"agent_name": "live"}))
    (directory / "200.json").write_text(json.dumps({"agent_name": "dead"}))

    registry.prune_dead_pids(is_alive=lambda pid: pid == 100)

    assert (directory / "100.json").exists()
    assert not (directory / "200.json").exists()


def test_record_prunes_dead_entries_but_preserves_the_new_one(runtime: Path) -> None:
    directory = registry.agent_pids_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "999.json").write_text(json.dumps({"agent_name": "stale"}))

    # The pid being recorded is treated as live by the real /proc-less default
    # only when it actually exists; here we just assert the new entry survives the
    # internal prune even though the recorded pid (123) is not a running process.
    registry.record_agent_pid(123, "beta", is_worker=False)

    assert registry.lookup_agent(123) is not None
