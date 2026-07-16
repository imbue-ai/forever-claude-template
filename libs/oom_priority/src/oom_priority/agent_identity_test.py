import json
from pathlib import Path

import pytest

from oom_priority.agent_identity import is_chat_agent, is_primary_agent, is_worker_agent


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
    # A miss returns False for every classifier; the launch wrapper folds
    # "not primary, not chat, not worker" into the least-protected agent tier.
    assert is_worker_agent("missing") is False


def test_user_created_label_is_a_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    _write_agent(tmp_path, "id-1", "chat", {"user_created": "true"})
    assert is_chat_agent("chat") is True


def test_worker_is_not_a_chat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    _write_agent(tmp_path, "id-1", "fixer", {"agent_created": "true"})
    assert is_chat_agent("fixer") is False


def test_unknown_agent_is_not_a_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    _write_agent(tmp_path, "id-1", "someone-else", {"user_created": "true"})
    # An agent we have no record for is not positively a chat, so it does not get
    # a chat's engagement-based protection -- it falls to least-protected instead.
    assert is_chat_agent("missing") is False


def test_no_host_dir_defaults_to_not_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MNGR_HOST_DIR", raising=False)
    assert is_worker_agent("anything") is False


def test_is_primary_label_is_recognised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    _write_agent(
        tmp_path, "id-1", "services", {"is_primary": "true", "user_created": "true"}
    )
    assert is_primary_agent("services") is True
    # The primary agent is not a worker -- the two classes are disjoint.
    assert is_worker_agent("services") is False


def test_non_primary_agents_are_not_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    _write_agent(tmp_path, "id-1", "chat", {"user_created": "true"})
    assert is_primary_agent("chat") is False


def test_unknown_agent_is_not_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    _write_agent(tmp_path, "id-1", "someone-else", {"is_primary": "true"})
    # Only an agent we can positively identify as primary is pinned; a miss falls
    # back to the ordinary (shed-able) default rather than accidentally pinning.
    assert is_primary_agent("missing") is False
