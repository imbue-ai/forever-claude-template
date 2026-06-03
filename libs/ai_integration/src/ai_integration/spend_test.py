import pytest

from ai_integration.errors import SpendCeilingExceededError
from ai_integration.spend import SpendTracker


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_record_and_spent_in_window(tmp_path) -> None:
    tracker = SpendTracker(
        "svc", ceiling_usd=10.0, state_root=tmp_path, window_seconds=100, clock=_Clock()
    )
    tracker.record(2.0)
    tracker.record(3.0)
    assert tracker.spent_in_window() == 5.0


def test_window_prunes_old_entries(tmp_path) -> None:
    clock = _Clock()
    tracker = SpendTracker(
        "svc", ceiling_usd=10.0, state_root=tmp_path, window_seconds=100, clock=clock
    )
    tracker.record(4.0)
    clock.t += 200  # advance past the window
    assert tracker.spent_in_window() == 0.0


def test_check_ceiling_escalates_and_raises(tmp_path) -> None:
    messages: list[str] = []
    tracker = SpendTracker(
        "svc",
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
        "svc", ceiling_usd=5.0, state_root=tmp_path, window_seconds=100, clock=_Clock()
    )
    tracker.record(2.0)
    tracker.check_ceiling()  # must not raise


def test_spend_persists_across_instances(tmp_path) -> None:
    clock = _Clock()
    first = SpendTracker(
        "svc", ceiling_usd=10.0, state_root=tmp_path, window_seconds=1000, clock=clock
    )
    first.record(3.0)
    second = SpendTracker(
        "svc", ceiling_usd=10.0, state_root=tmp_path, window_seconds=1000, clock=clock
    )
    assert second.spent_in_window() == 3.0
