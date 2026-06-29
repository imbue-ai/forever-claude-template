from oom_priority.process_tree import find_claude_ancestor, is_claude_process


def test_is_claude_process_matches_binary_not_the_python_hook() -> None:
    # The claude agent process.
    assert is_claude_process("claude", "claude")
    assert is_claude_process("claude", "node")
    # The hook's own process must NOT match: only the script path contains
    # "claude", while its comm and argv[0] basename are "python3".
    assert not is_claude_process("python3", "python3")
    assert not is_claude_process("bash", "bash")


def _tree(parents: dict[int, int], comms: dict[int, str]):
    """Build injectable /proc readers from a pid->ppid map and pid->comm map."""

    def ppid_of(pid: int) -> int | None:
        return parents.get(pid)

    def comm_of(pid: int) -> str:
        return comms.get(pid, "")

    def argv0_of(pid: int) -> str:
        return comms.get(pid, "")

    return ppid_of, comm_of, argv0_of


def test_find_claude_ancestor_walks_up_to_the_agent_process() -> None:
    # hook(10) -> shell(9) -> claude(8) -> pane shell(7)
    parents = {10: 9, 9: 8, 8: 7, 7: 1}
    comms = {10: "python3", 9: "bash", 8: "claude", 7: "bash"}
    ppid_of, comm_of, argv0_of = _tree(parents, comms)
    assert find_claude_ancestor(10, ppid_of, comm_of, argv0_of) == 8


def test_find_claude_ancestor_returns_none_when_absent() -> None:
    parents = {10: 9, 9: 8, 8: 1}
    comms = {10: "python3", 9: "bash", 8: "bash"}
    ppid_of, comm_of, argv0_of = _tree(parents, comms)
    assert find_claude_ancestor(10, ppid_of, comm_of, argv0_of) is None


def test_find_claude_ancestor_stops_at_pid_1_and_broken_chains() -> None:
    # ppid_of returns None mid-walk (unreadable /proc) -> give up, no crash.
    parents: dict[int, int] = {10: 9}
    comms = {10: "python3", 9: "bash"}
    ppid_of, comm_of, argv0_of = _tree(parents, comms)
    assert find_claude_ancestor(10, ppid_of, comm_of, argv0_of) is None


def test_find_claude_ancestor_does_not_loop_on_a_cycle() -> None:
    # A self-parent cycle must terminate rather than spin.
    parents = {5: 5}
    comms = {5: "bash"}
    ppid_of, comm_of, argv0_of = _tree(parents, comms)
    assert find_claude_ancestor(5, ppid_of, comm_of, argv0_of) is None
