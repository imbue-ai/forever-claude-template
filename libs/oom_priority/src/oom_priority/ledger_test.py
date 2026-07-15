from pathlib import Path

import pytest

from oom_priority import ledger


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OOM_PRIORITY_RUNTIME_DIR", str(tmp_path))
    return tmp_path


def test_shed_record_round_trip_distinguishes_agent_from_subprocess(
    runtime: Path,
) -> None:
    ledger.append_shed_record(
        pid=111, comm="claude", agent_name="alpha", is_worker=True
    )
    ledger.append_shed_record(pid=222, comm="pytest", agent_name=None, is_worker=None)

    records = ledger.read_records()
    assert [r["pid"] for r in records] == [111, 222]
    agent_record, sub_record = records
    assert agent_record["agent_name"] == "alpha" and agent_record["is_worker"] is True
    # A shed subprocess carries no agent identity, so it never triggers a revival.
    assert sub_record["agent_name"] is None


def test_pending_shed_is_cleared_by_a_delivered_notice(runtime: Path) -> None:
    ledger.append_shed_record(pid=1, comm="claude", agent_name="alpha", is_worker=False)
    first = ledger.pending_shed_timestamps(ledger.read_records(), "alpha")
    assert len(first) == 1
    assert ledger.has_pending_shed("alpha")

    # Acknowledge through the latest shed -> nothing pending anymore.
    ledger.append_notice_delivered("alpha", max(first))
    assert ledger.pending_shed_timestamps(ledger.read_records(), "alpha") == []
    assert not ledger.has_pending_shed("alpha")


def test_a_new_shed_after_delivery_is_pending_again(runtime: Path) -> None:
    ledger.append_shed_record(pid=1, comm="claude", agent_name="alpha", is_worker=False)
    ledger.append_notice_delivered(
        "alpha", max(ledger.pending_shed_timestamps(ledger.read_records(), "alpha"))
    )
    # A second shed, later than the delivered marker, is pending once more.
    ledger.append_shed_record(pid=2, comm="claude", agent_name="alpha", is_worker=False)
    assert ledger.has_pending_shed("alpha")


def test_pending_shed_is_scoped_to_the_named_agent(runtime: Path) -> None:
    ledger.append_shed_record(pid=1, comm="claude", agent_name="alpha", is_worker=False)
    assert not ledger.has_pending_shed("beta")


def test_read_records_skips_malformed_lines(runtime: Path) -> None:
    path = ledger.shed_ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"type": "process_shed", "pid": 1}\nnot json\n\n{"pid": 2}\n')
    records = ledger.read_records()
    assert [r.get("pid") for r in records] == [1, 2]
