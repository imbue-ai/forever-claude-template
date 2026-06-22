from memory_watchdog.classifier import classify_processes, find_services_session_name
from memory_watchdog.data_types import ProcessInfo, Tier, TmuxPane

_SERVICES_SESSION = "mngr-services"
_PREFIX = "mngr-"

_SYSTEM_INTERFACE_CMD = (
    'bash -c "python3 scripts/forward_port.py --url http://localhost:8000 '
    '--name system_interface && system-interface"'
)


def _tier_by_pid(classifications) -> dict[int, Tier]:
    return {c.pid: c.tier for c in classifications}


def _label_by_pid(classifications) -> dict[int, str]:
    return {c.pid: c.label for c in classifications}


def _build_standard_tree() -> tuple[list[ProcessInfo], list[TmuxPane]]:
    """A representative supervisord-era container.

    The services session has one ``bootstrap`` window whose pane runs the shell
    that exec'd supervisord; every background service is a supervisord child (not
    its own tmux window). The session also has the services agent's idle window.
    Two agent sessions (a user agent with a tool subprocess, and a worker) round
    it out.
    """
    processes = [
        # Infrastructure not under any pane.
        ProcessInfo(pid=1, parent_pid=0, resident_kb=1000, command_line="/sbin/init"),
        ProcessInfo(pid=10, parent_pid=1, resident_kb=2000, command_line="tmux"),
        ProcessInfo(
            pid=11, parent_pid=1, resident_kb=1500, command_line="/usr/sbin/sshd"
        ),
        # Services session, "bootstrap" window: shell -> supervisord -> services.
        ProcessInfo(pid=100, parent_pid=10, resident_kb=500, command_line="bash"),
        ProcessInfo(
            pid=102,
            parent_pid=100,
            resident_kb=4000,
            command_line="supervisord -n -c supervisord.conf",
        ),
        # supervisord children (one per service), some with a grandchild.
        ProcessInfo(
            pid=110,
            parent_pid=102,
            resident_kb=2000,
            command_line=_SYSTEM_INTERFACE_CMD,
        ),
        ProcessInfo(
            pid=111, parent_pid=110, resident_kb=80000, command_line="system-interface"
        ),
        ProcessInfo(
            pid=120, parent_pid=102, resident_kb=20000, command_line="uv run web-server"
        ),
        ProcessInfo(
            pid=130,
            parent_pid=102,
            resident_kb=15000,
            command_line="uv run memory-watchdog",
        ),
        ProcessInfo(
            pid=140,
            parent_pid=102,
            resident_kb=12000,
            command_line="uv run host-backup",
        ),
        ProcessInfo(
            pid=150,
            parent_pid=102,
            resident_kb=3000,
            command_line="bash scripts/run_ttyd.sh",
        ),
        ProcessInfo(
            pid=160, parent_pid=102, resident_kb=30000, command_line="my-dashboard"
        ),
        # Services session, the services agent's own idle window.
        ProcessInfo(pid=170, parent_pid=10, resident_kb=500, command_line="bash"),
        ProcessInfo(
            pid=171, parent_pid=170, resident_kb=120000, command_line="node claude"
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
        TmuxPane(session_name=_SERVICES_SESSION, window_name="bootstrap", pane_pid=100),
        TmuxPane(session_name=_SERVICES_SESSION, window_name="0", pane_pid=170),
        TmuxPane(session_name="mngr-alice", window_name="0", pane_pid=200),
        TmuxPane(session_name="mngr-worker7", window_name="0", pane_pid=300),
    ]
    return processes, panes


def _classify(processes, panes):
    return classify_processes(
        processes=processes,
        panes=panes,
        services_session_name=_SERVICES_SESSION,
        mngr_prefix=_PREFIX,
        user_created_agent_names=frozenset({"alice"}),
        agent_created_agent_names=frozenset({"worker7"}),
    )


def test_services_are_tiered_by_supervisord_child_command() -> None:
    processes, panes = _build_standard_tree()
    tier_by_pid = _tier_by_pid(_classify(processes, panes))

    # supervisord itself and the shell that launched it are never shed.
    assert tier_by_pid[100] == Tier.INFRASTRUCTURE
    assert tier_by_pid[102] == Tier.INFRASTRUCTURE
    # Each service is tiered by its command line; a service's whole subtree
    # (e.g. the system_interface bash wrapper plus the server) shares the tier.
    assert tier_by_pid[110] == Tier.USER_INTERFACE
    assert tier_by_pid[111] == Tier.USER_INTERFACE
    assert tier_by_pid[120] == Tier.AUXILIARY_SERVICE  # web
    assert tier_by_pid[130] == Tier.RECOVERY  # memory-watchdog
    assert tier_by_pid[140] == Tier.DURABILITY  # host-backup
    assert tier_by_pid[150] == Tier.USER_INTERFACE  # ttyd / terminal
    # An unrecognized supervisord child (agent-added) defaults to auxiliary.
    assert tier_by_pid[160] == Tier.AUXILIARY_SERVICE


def test_service_label_is_the_service_name_not_the_window() -> None:
    processes, panes = _build_standard_tree()
    label_by_pid = _label_by_pid(_classify(processes, panes))
    assert label_by_pid[111] == "system_interface"
    assert label_by_pid[120] == "web"
    assert label_by_pid[130] == "memory-watchdog"
    assert label_by_pid[150] == "terminal"


def test_services_agent_idle_window_is_protected() -> None:
    processes, panes = _build_standard_tree()
    tier_by_pid = _tier_by_pid(_classify(processes, panes))
    # The services agent's own idle shell and claude must never be shed -- they
    # keep the services session alive.
    assert tier_by_pid[170] == Tier.INFRASTRUCTURE
    assert tier_by_pid[171] == Tier.INFRASTRUCTURE


def test_agents_and_their_children() -> None:
    processes, panes = _build_standard_tree()
    tier_by_pid = _tier_by_pid(_classify(processes, panes))
    # Agent pane shells are spared so the session survives shedding.
    assert tier_by_pid[200] == Tier.INFRASTRUCTURE
    assert tier_by_pid[300] == Tier.INFRASTRUCTURE
    # The user's agent is tier 5; its tool subprocesses are tier 8.
    assert tier_by_pid[201] == Tier.USER_AGENT
    assert tier_by_pid[202] == Tier.AGENT_CHILD
    assert tier_by_pid[203] == Tier.AGENT_CHILD
    # The worker agent is tier 7.
    assert tier_by_pid[301] == Tier.WORKER_AGENT


def test_infrastructure_outside_any_pane() -> None:
    processes, panes = _build_standard_tree()
    tier_by_pid = _tier_by_pid(_classify(processes, panes))
    assert tier_by_pid[1] == Tier.INFRASTRUCTURE
    assert tier_by_pid[10] == Tier.INFRASTRUCTURE
    assert tier_by_pid[11] == Tier.INFRASTRUCTURE


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


def test_find_services_session_name_from_supervisord_ancestor() -> None:
    processes, panes = _build_standard_tree()
    # supervisord (102) descends from the bootstrap pane (100) in the services
    # session, so that is the services session.
    assert find_services_session_name(processes, panes) == _SERVICES_SESSION


def test_find_services_session_name_is_none_without_supervisord() -> None:
    processes = [
        ProcessInfo(pid=10, parent_pid=1, resident_kb=2000, command_line="tmux"),
        ProcessInfo(pid=200, parent_pid=10, resident_kb=500, command_line="bash"),
    ]
    panes = [TmuxPane(session_name="mngr-alice", window_name="0", pane_pid=200)]
    assert find_services_session_name(processes, panes) is None
