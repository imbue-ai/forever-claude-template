import json
from pathlib import Path

import pytest

from oom_priority.agent_identity import is_worker_agent


def _write_agent(host_dir: Path, agent_id: str, name: str, labels: dict) -> None:
    agent_dir = host_dir / "agents" / agent_id
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "data.json").write_text(json.dumps({"name": name, "labels": labels}))


def test_agent_created_label_is_a_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    _write_agent(tmp_path, "id-1", "fixer", {"agent_created": "true"})
    assert is_worker_agent("fixer") is True


def test_user_created_label_is_not_a_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    _write_agent(tmp_path, "id-1", "chat", {"user_created": "true"})
    assert is_worker_agent("chat") is False


def test_unknown_agent_defaults_to_not_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    _write_agent(tmp_path, "id-1", "someone-else", {"agent_created": "true"})
    # The protective default: an agent we have no record for is treated as a
    # user agent (more protected), not a worker.
    assert is_worker_agent("missing") is False


def test_no_host_dir_defaults_to_not_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MNGR_HOST_DIR", raising=False)
    assert is_worker_agent("anything") is False
