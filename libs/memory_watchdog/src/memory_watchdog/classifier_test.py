from memory_watchdog.classifier import classify_processes
from memory_watchdog.data_types import ProcessInfo, Tier, TmuxPane

_SERVICES_SESSION = "mngr-services"
_PREFIX = "mngr-"


def _tier_by_pid(classifications) -> dict[int, Tier]:
    return {c.pid: c.tier for c in classifications}


def _build_standard_tree() -> tuple[list[ProcessInfo], list[TmuxPane]]:
    """A representative container: infra, a service, a user agent, a worker."""
    processes = [
        # Infrastructure not under any pane.
        ProcessInfo(pid=1, parent_pid=0, resident_kb=1000, command_line="/sbin/init"),
        ProcessInfo(pid=10, parent_pid=1, resident_kb=2000, command_line="tmux"),
        ProcessInfo(
            pid=11, parent_pid=1, resident_kb=1500, command_line="/usr/sbin/sshd"
        ),
        # Services session: system_interface window.
        ProcessInfo(pid=100, parent_pid=10, resident_kb=500, command_line="bash"),
        ProcessInfo(
            pid=101,
            parent_pid=100,
            resident_kb=80000,
            command_line="python system-interface",
        ),
        # Services session: web window (sheddable tier 6).
        ProcessInfo(pid=120, parent_pid=10, resident_kb=500, command_line="bash"),
        ProcessInfo(
            pid=121, parent_pid=120, resident_kb=20000, command_line="uv run web-server"
        ),
        # User agent session: claude + a tool subprocess running pytest.
        ProcessInfo(pid=200, parent_pid=10, resident_kb=500, command_line="bash"),
        ProcessInfo(
            pid=201, parent_pid=200, resident_kb=300000, command_line="node claude"
        ),
        ProcessInfo(
            pid=202, parent_pid=201, resident_kb=8000, command_line="bash -c pytest"
        ),
        ProcessInfo(
            pid=203, parent_pid=202, resident_kb=500000, command_line="/usr/bin/pytest"
        ),
        # Worker agent session: just claude.
        ProcessInfo(pid=300, parent_pid=10, resident_kb=500, command_line="bash"),
        ProcessInfo(
            pid=301, parent_pid=300, resident_kb=250000, command_line="node claude"
        ),
    ]
    panes = [
        TmuxPane(
            session_name=_SERVICES_SESSION,
            window_name="svc-system_interface",
            pane_pid=100,
        ),
        TmuxPane(session_name=_SERVICES_SESSION, window_name="svc-web", pane_pid=120),
        TmuxPane(session_name="mngr-alice", window_name="0", pane_pid=200),
        TmuxPane(session_name="mngr-worker7", window_name="0", pane_pid=300),
    ]
    return processes, panes


def test_classifies_each_tier_correctly() -> None:
    processes, panes = _build_standard_tree()
    classifications = classify_processes(
        processes=processes,
        panes=panes,
        services_session_name=_SERVICES_SESSION,
        mngr_prefix=_PREFIX,
        user_created_agent_names=frozenset({"alice"}),
        agent_created_agent_names=frozenset({"worker7"}),
    )
    tier_by_pid = _tier_by_pid(classifications)

    # Infrastructure outside any pane.
    assert tier_by_pid[1] == Tier.INFRASTRUCTURE
    assert tier_by_pid[10] == Tier.INFRASTRUCTURE
    assert tier_by_pid[11] == Tier.INFRASTRUCTURE
    # Pane shells are always spared (infrastructure), so windows survive shedding.
    assert tier_by_pid[100] == Tier.INFRASTRUCTURE
    assert tier_by_pid[120] == Tier.INFRASTRUCTURE
    assert tier_by_pid[200] == Tier.INFRASTRUCTURE
    assert tier_by_pid[300] == Tier.INFRASTRUCTURE
    # The system interface server is protected.
    assert tier_by_pid[101] == Tier.USER_INTERFACE
    # The web service is an auxiliary, sheddable service.
    assert tier_by_pid[121] == Tier.AUXILIARY_SERVICE
    # The user's agent process is tier 5; its tool subprocesses are tier 8.
    assert tier_by_pid[201] == Tier.USER_AGENT
    assert tier_by_pid[202] == Tier.AGENT_CHILD
    assert tier_by_pid[203] == Tier.AGENT_CHILD
    # The worker agent is tier 7.
    assert tier_by_pid[301] == Tier.WORKER_AGENT


def test_unlabeled_agent_defaults_to_user_agent_protective() -> None:
    processes = [
        ProcessInfo(pid=10, parent_pid=1, resident_kb=2000, command_line="tmux"),
        ProcessInfo(pid=200, parent_pid=10, resident_kb=500, command_line="bash"),
        ProcessInfo(
            pid=201, parent_pid=200, resident_kb=300000, command_line="node claude"
        ),
    ]
    panes = [TmuxPane(session_name="mngr-mystery", window_name="0", pane_pid=200)]
    classifications = classify_processes(
        processes=processes,
        panes=panes,
        services_session_name=_SERVICES_SESSION,
        mngr_prefix=_PREFIX,
        user_created_agent_names=frozenset(),
        agent_created_agent_names=frozenset(),
    )
    # No label either way -> protect it at tier 5 rather than risk shedding a
    # user's agent early.
    assert _tier_by_pid(classifications)[201] == Tier.USER_AGENT


def test_agent_added_service_defaults_to_auxiliary() -> None:
    processes = [
        ProcessInfo(pid=10, parent_pid=1, resident_kb=2000, command_line="tmux"),
        ProcessInfo(pid=400, parent_pid=10, resident_kb=500, command_line="bash"),
        ProcessInfo(
            pid=401, parent_pid=400, resident_kb=30000, command_line="my-dashboard"
        ),
    ]
    panes = [
        TmuxPane(
            session_name=_SERVICES_SESSION, window_name="svc-my-dashboard", pane_pid=400
        ),
    ]
    classifications = classify_processes(
        processes=processes,
        panes=panes,
        services_session_name=_SERVICES_SESSION,
        mngr_prefix=_PREFIX,
        user_created_agent_names=frozenset(),
        agent_created_agent_names=frozenset(),
    )
    assert _tier_by_pid(classifications)[401] == Tier.AUXILIARY_SERVICE


def test_recovery_and_durability_windows() -> None:
    processes = [
        ProcessInfo(pid=10, parent_pid=1, resident_kb=2000, command_line="tmux"),
        ProcessInfo(pid=500, parent_pid=10, resident_kb=500, command_line="bash"),
        ProcessInfo(
            pid=501, parent_pid=500, resident_kb=10000, command_line="uv run bootstrap"
        ),
        ProcessInfo(pid=510, parent_pid=10, resident_kb=500, command_line="bash"),
        ProcessInfo(
            pid=511,
            parent_pid=510,
            resident_kb=10000,
            command_line="uv run host-backup",
        ),
        ProcessInfo(pid=520, parent_pid=10, resident_kb=500, command_line="bash"),
        ProcessInfo(
            pid=521,
            parent_pid=520,
            resident_kb=10000,
            command_line="uv run memory-watchdog",
        ),
    ]
    panes = [
        TmuxPane(session_name=_SERVICES_SESSION, window_name="bootstrap", pane_pid=500),
        TmuxPane(
            session_name=_SERVICES_SESSION, window_name="svc-host-backup", pane_pid=510
        ),
        TmuxPane(
            session_name=_SERVICES_SESSION,
            window_name="svc-memory-watchdog",
            pane_pid=520,
        ),
    ]
    classifications = classify_processes(
        processes=processes,
        panes=panes,
        services_session_name=_SERVICES_SESSION,
        mngr_prefix=_PREFIX,
        user_created_agent_names=frozenset(),
        agent_created_agent_names=frozenset(),
    )
    tier_by_pid = _tier_by_pid(classifications)
    assert tier_by_pid[501] == Tier.RECOVERY
    assert tier_by_pid[521] == Tier.RECOVERY
    assert tier_by_pid[511] == Tier.DURABILITY
