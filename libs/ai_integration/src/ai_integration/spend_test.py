import pytest

from ai_integration.errors import SpendCeilingExceededError
from ai_integration.spend import (
    DEFAULT_WINDOW_SECONDS,
    SpendTracker,
    load_spend_tracker,
)


def _write_toml(tmp_path, body: str):
    path = tmp_path / "services.toml"
    path.write_text(body, encoding="utf-8")
    return path


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_record_and_spent_in_window(tmp_path) -> None:
    tracker = SpendTracker(
        service_name="svc",
        ceiling_usd=10.0,
        state_root=tmp_path,
        window_seconds=100,
        clock=_Clock(),
    )
    tracker.record(2.0)
    tracker.record(3.0)
    assert tracker.spent_in_window() == 5.0


def test_window_prunes_old_entries(tmp_path) -> None:
    clock = _Clock()
    tracker = SpendTracker(
        service_name="svc",
        ceiling_usd=10.0,
        state_root=tmp_path,
        window_seconds=100,
        clock=clock,
    )
    tracker.record(4.0)
    clock.t += 200  # advance past the window
    assert tracker.spent_in_window() == 0.0


def test_check_ceiling_escalates_and_raises(tmp_path) -> None:
    messages: list[str] = []
    tracker = SpendTracker(
        service_name="svc",
        ceiling_usd=5.0,
        state_root=tmp_path,
        window_seconds=100,
        clock=_Clock(),
        escalate=messages.append,
    )
    tracker.record(5.0)
    with pytest.raises(SpendCeilingExceededError):
        tracker.check_ceiling()
    assert messages
    assert "svc" in messages[0]


def test_check_ceiling_ok_under_budget(tmp_path) -> None:
    tracker = SpendTracker(
        service_name="svc",
        ceiling_usd=5.0,
        state_root=tmp_path,
        window_seconds=100,
        clock=_Clock(),
    )
    tracker.record(2.0)
    tracker.check_ceiling()  # must not raise


def test_corrupt_ledger_is_tolerated_as_empty(tmp_path) -> None:
    tracker = SpendTracker(
        service_name="svc",
        ceiling_usd=10.0,
        state_root=tmp_path,
        window_seconds=1000,
        clock=_Clock(),
    )
    ledger = tmp_path / "svc" / "ai_spend.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("{not valid json")
    # A corrupt ledger must not crash; it reads as empty spend.
    assert tracker.spent_in_window() == 0.0


def test_spend_persists_across_instances(tmp_path) -> None:
    clock = _Clock()
    first = SpendTracker(
        service_name="svc",
        ceiling_usd=10.0,
        state_root=tmp_path,
        window_seconds=1000,
        clock=clock,
    )
    first.record(3.0)
    second = SpendTracker(
        service_name="svc",
        ceiling_usd=10.0,
        state_root=tmp_path,
        window_seconds=1000,
        clock=clock,
    )
    assert second.spent_in_window() == 3.0


def test_load_spend_tracker_reads_ceiling_and_window(tmp_path) -> None:
    toml = _write_toml(
        tmp_path,
        """
[services.triage]
command = "uv run triage"

[services.triage.ai_spend]
ceiling_usd = 7.5
window_seconds = 3600
""",
    )
    tracker = load_spend_tracker("triage", services_toml_path=toml, state_root=tmp_path)
    assert tracker is not None
    assert tracker.ceiling_usd == 7.5
    assert tracker.window_seconds == 3600
    assert tracker.service_name == "triage"


def test_load_spend_tracker_works_without_command(tmp_path) -> None:
    # A service that needs spend tracking but is not a continuously-running
    # background process can declare only the ai_spend table (no command).
    toml = _write_toml(
        tmp_path,
        """
[services.batch-job.ai_spend]
ceiling_usd = 2.0
""",
    )
    tracker = load_spend_tracker(
        "batch-job", services_toml_path=toml, state_root=tmp_path
    )
    assert tracker is not None
    assert tracker.ceiling_usd == 2.0
    # window_seconds omitted -> defaults to 24h.
    assert tracker.window_seconds == DEFAULT_WINDOW_SECONDS


def test_load_spend_tracker_none_when_unconfigured(tmp_path) -> None:
    toml = _write_toml(
        tmp_path,
        """
[services.triage]
command = "uv run triage"
""",
    )
    # No ai_spend table -> spend tracking off for that service.
    assert (
        load_spend_tracker("triage", services_toml_path=toml, state_root=tmp_path)
        is None
    )
    # Unknown service -> None.
    assert (
        load_spend_tracker("ghost", services_toml_path=toml, state_root=tmp_path)
        is None
    )
    # Missing file -> None.
    assert (
        load_spend_tracker(
            "triage", services_toml_path=tmp_path / "nope.toml", state_root=tmp_path
        )
        is None
    )


def test_load_spend_tracker_none_when_ceiling_missing(tmp_path) -> None:
    # An ai_spend table without a numeric ceiling_usd is a misconfiguration; it
    # disables tracking (and logs) rather than guessing a ceiling.
    toml = _write_toml(
        tmp_path,
        """
[services.triage.ai_spend]
window_seconds = 3600
""",
    )
    assert (
        load_spend_tracker("triage", services_toml_path=toml, state_root=tmp_path)
        is None
    )
