from memory_watchdog.data_types import (
    MemoryPressure,
    ProcessInfo,
    ShedRecord,
    Tier,
    TmuxPane,
)
from memory_watchdog.watchdog import _build_status, _windows_with_no_live_process

_SERVICES_SESSION = "mngr-services"
_SUPERVISED = frozenset({"bootstrap", "telegram", "terminal"})


def test_window_with_a_live_child_is_not_dead() -> None:
    panes = [
        TmuxPane(session_name=_SERVICES_SESSION, window_name="bootstrap", pane_pid=100)
    ]
    processes = [
        ProcessInfo(pid=100, parent_pid=10, resident_kb=500, command_line="bash"),
        ProcessInfo(
            pid=101, parent_pid=100, resident_kb=5000, command_line="uv run bootstrap"
        ),
    ]
    dead = _windows_with_no_live_process(
        panes, processes, _SERVICES_SESSION, _SUPERVISED
    )
    assert dead == set()


def test_window_with_only_an_idle_shell_is_dead() -> None:
    panes = [
        TmuxPane(session_name=_SERVICES_SESSION, window_name="bootstrap", pane_pid=100)
    ]
    processes = [
        ProcessInfo(pid=100, parent_pid=10, resident_kb=500, command_line="bash")
    ]
    dead = _windows_with_no_live_process(
        panes, processes, _SERVICES_SESSION, _SUPERVISED
    )
    assert dead == {"bootstrap"}


def test_unsupervised_and_other_session_windows_are_ignored() -> None:
    panes = [
        # Idle, but not a supervised window.
        TmuxPane(session_name=_SERVICES_SESSION, window_name="svc-web", pane_pid=200),
        # A supervised name, but in an agent session (not the services session).
        TmuxPane(session_name="mngr-alice", window_name="terminal", pane_pid=300),
    ]
    processes = [
        ProcessInfo(pid=200, parent_pid=10, resident_kb=500, command_line="bash"),
        ProcessInfo(pid=300, parent_pid=10, resident_kb=500, command_line="bash"),
    ]
    dead = _windows_with_no_live_process(
        panes, processes, _SERVICES_SESSION, _SUPERVISED
    )
    assert dead == set()


def test_exec_style_service_without_children_is_not_dead() -> None:
    # ttyd exec's into the pane, so the pane process *is* ttyd (not a shell) and
    # has no child when no client is attached -- it must not be flagged dead.
    panes = [
        TmuxPane(session_name=_SERVICES_SESSION, window_name="terminal", pane_pid=400)
    ]
    processes = [
        ProcessInfo(
            pid=400, parent_pid=10, resident_kb=3000, command_line="ttyd -p 7681"
        )
    ]
    dead = _windows_with_no_live_process(
        panes, processes, _SERVICES_SESSION, _SUPERVISED
    )
    assert dead == set()


def _pressure(used_fraction: float) -> MemoryPressure:
    total = 1_000_000
    return MemoryPressure(
        total_kb=total, available_kb=int(total * (1.0 - used_fraction))
    )


def test_status_under_pressure_when_usage_high() -> None:
    status = _build_status(_pressure(0.95), (), (), "2026-06-12T10:00:00.000000000Z")
    assert status.is_under_pressure is True


def test_status_under_pressure_when_recent_sheds_even_if_usage_low() -> None:
    record = ShedRecord(
        timestamp="2026-06-12T10:00:00.000000000Z",
        tier=Tier.AGENT_CHILD,
        tier_rank=8,
        label="pytest",
        pid=1,
        resident_kb=1000,
        agent_name=None,
    )
    status = _build_status(
        _pressure(0.10), (record,), (), "2026-06-12T10:00:00.000000000Z"
    )
    assert status.is_under_pressure is True
    assert status.recently_shed[0].label == "pytest"


def test_status_not_under_pressure_when_calm() -> None:
    status = _build_status(_pressure(0.10), (), (), "2026-06-12T10:00:00.000000000Z")
    assert status.is_under_pressure is False
