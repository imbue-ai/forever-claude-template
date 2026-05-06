"""Unit tests for the bootstrap service manager's reconciliation logic."""

from bootstrap.manager import _compute_actions


def test_compute_actions_no_changes_when_in_sync() -> None:
    desired = {"a": {"command": "cmd-a", "restart": "never"}}
    current = {"a": {"window_name": "svc-a", "command": "cmd-a"}}
    stops, starts = _compute_actions(desired, current)
    assert stops == []
    assert starts == []


def test_compute_actions_starts_missing_service() -> None:
    desired = {"a": {"command": "cmd-a", "restart": "never"}}
    current: dict[str, dict[str, str]] = {}
    stops, starts = _compute_actions(desired, current)
    assert stops == []
    assert starts == [("a", "cmd-a")]


def test_compute_actions_stops_removed_service() -> None:
    desired: dict[str, dict] = {}
    current = {"a": {"window_name": "svc-a", "command": "cmd-a"}}
    stops, starts = _compute_actions(desired, current)
    assert stops == ["a"]
    assert starts == []


def test_compute_actions_restarts_on_command_change() -> None:
    desired = {"a": {"command": "cmd-a-new", "restart": "never"}}
    current = {"a": {"window_name": "svc-a", "command": "cmd-a-old"}}
    stops, starts = _compute_actions(desired, current)
    assert stops == ["a"]
    assert starts == [("a", "cmd-a-new")]


def test_compute_actions_treats_unknown_recorded_command_as_change() -> None:
    # A window created by an older manager has no recorded command; reading the
    # user-option yields "". That mismatch should trigger a restart so the new
    # manager takes ownership of the window with a known command.
    desired = {"a": {"command": "cmd-a", "restart": "never"}}
    current = {"a": {"window_name": "svc-a", "command": ""}}
    stops, starts = _compute_actions(desired, current)
    assert stops == ["a"]
    assert starts == [("a", "cmd-a")]


def test_compute_actions_handles_mixed_add_remove_change() -> None:
    desired = {
        "keep": {"command": "k", "restart": "never"},
        "change": {"command": "new", "restart": "never"},
        "add": {"command": "added", "restart": "never"},
    }
    current = {
        "keep": {"window_name": "svc-keep", "command": "k"},
        "change": {"window_name": "svc-change", "command": "old"},
        "remove": {"window_name": "svc-remove", "command": "r"},
    }
    stops, starts = _compute_actions(desired, current)
    assert sorted(stops) == ["change", "remove"]
    assert sorted(starts) == [("add", "added"), ("change", "new")]
