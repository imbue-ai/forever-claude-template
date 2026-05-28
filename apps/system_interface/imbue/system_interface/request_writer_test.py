"""Tests for request_writer module."""

import json
from pathlib import Path

import pytest

from imbue.system_interface.request_writer import write_refresh_request


def test_write_refresh_request_writes_jsonl_to_correct_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))

    write_refresh_request("web")

    events_file = tmp_path / "events" / "refresh" / "events.jsonl"
    assert events_file.exists()
    lines = events_file.read_text().splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["type"] == "refresh_service"
    assert event["source"] == "refresh"
    assert event["service_name"] == "web"
    assert event["event_id"].startswith("evt-")
    assert "timestamp" in event


def test_write_refresh_request_appends_multiple_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))

    write_refresh_request("web")
    write_refresh_request("api")

    events_file = tmp_path / "events" / "refresh" / "events.jsonl"
    lines = events_file.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["service_name"] == "web"
    assert json.loads(lines[1])["service_name"] == "api"


def test_write_refresh_request_without_agent_state_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MNGR_AGENT_STATE_DIR", raising=False)
    with pytest.raises(RuntimeError, match="MNGR_AGENT_STATE_DIR"):
        write_refresh_request("web")
