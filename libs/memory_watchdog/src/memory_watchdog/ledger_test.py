import json
from pathlib import Path

import pytest

from memory_watchdog.data_types import MemoryStatus, RecentShedSummary, ShedRecord, Tier
from memory_watchdog.ledger import (
    append_shed_records,
    read_currently_blocked_services,
    record_service_blocked,
    record_service_unblocked,
    shed_ledger_path,
    status_path,
    write_status,
)


@pytest.fixture(autouse=True)
def _redirect_runtime_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORY_WATCHDOG_RUNTIME_DIR", str(tmp_path / "memory_watchdog"))


def test_append_shed_records_writes_one_jsonl_line_each() -> None:
    records = [
        ShedRecord(
            timestamp="2026-06-12T10:00:00.000000000Z",
            tier=Tier.AGENT_CHILD,
            tier_rank=8,
            label="pytest",
            pid=42,
            resident_kb=500000,
            agent_name=None,
        ),
        ShedRecord(
            timestamp="2026-06-12T10:00:01.000000000Z",
            tier=Tier.AGENT_CHILD,
            tier_rank=8,
            label="python3 build.py",
            pid=43,
            resident_kb=250000,
            agent_name=None,
            owning_agent_name="worker7",
        ),
    ]
    append_shed_records(records)
    lines = shed_ledger_path().read_text().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["type"] == "process_shed"
    assert first["label"] == "pytest"
    assert first["agent_name"] is None
    # The owning agent is persisted so the durable ledger is not lossier than
    # the live status file (a subprocess shed carries its owning agent even
    # though agent_name -- which drives the revival notice -- stays None).
    assert first["owning_agent_name"] is None
    second = json.loads(lines[1])
    assert second["agent_name"] is None
    assert second["owning_agent_name"] == "worker7"


def test_blocked_services_reflect_block_then_unblock() -> None:
    record_service_blocked("web", "crash-looped")
    record_service_blocked("telegram", "crash-looped")
    assert read_currently_blocked_services() == ["telegram", "web"]
    record_service_unblocked("web")
    assert read_currently_blocked_services() == ["telegram"]


def test_blocked_services_empty_when_no_ledger() -> None:
    assert read_currently_blocked_services() == []


def test_write_status_round_trips() -> None:
    status = MemoryStatus(
        timestamp="2026-06-12T10:00:00.000000000Z",
        used_fraction=0.93,
        total_kb=4_000_000,
        available_kb=280_000,
        pressure_threshold_fraction=0.90,
        is_under_pressure=True,
        recently_shed=(
            RecentShedSummary(
                label="pytest", tier_rank=8, count=2, reclaimed_kb=500000
            ),
        ),
        blocked_services=("web",),
    )
    write_status(status)
    written = json.loads(status_path().read_text())
    assert written["is_under_pressure"] is True
    assert written["recently_shed"][0]["label"] == "pytest"
    assert written["blocked_services"] == ["web"]
