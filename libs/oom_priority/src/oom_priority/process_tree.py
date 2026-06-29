"""Locate an agent's main ``claude`` process by walking up the process tree.

A Claude hook runs as a short-lived descendant of the long-lived ``claude``
agent process. To tag the agent (and register its pid) we must find that claude
process among the hook's ancestors -- tagging the hook's own shell would do
nothing once the hook exits, since ``oom_score_adj`` is inherited by *future*
children from the process we tag.

The walk and the "is this claude?" test are kept as a pure function with the
``/proc`` readers injected, so it is unit-testable without a real process tree.

Stdlib-only (see ``paths``).
"""

from collections.abc import Callable
from typing import Final

# How far up the ancestry to look before giving up. The claude process is only a
# few hops above the hook (hook -> [shell] -> claude); a generous cap guards
# against an unexpected tree without ever looping forever.
_MAX_ANCESTRY_DEPTH: Final[int] = 12


def is_claude_process(comm: str, argv0_basename: str) -> bool:
    """Whether a process looks like the claude agent binary.

    Matches the native ``claude`` binary by its comm or argv[0] basename. The
    hook's own process (``python3 .../claude_oom_tag_agent.py``) is deliberately
    not matched: its comm is ``python3`` and its argv[0] basename is ``python3``,
    not ``claude`` -- only the script *path* contains "claude".
    """
    return comm == "claude" or argv0_basename == "claude"


def find_claude_ancestor(
    start_pid: int,
    ppid_of: Callable[[int], int | None],
    comm_of: Callable[[int], str],
    argv0_basename_of: Callable[[int], str],
) -> int | None:
    """Walk up from ``start_pid`` and return the first claude ancestor's pid.

    Returns None if no claude process is found within the depth cap (in which
    case the caller no-ops: the agent keeps its inherited band, which is the safe
    protected default).
    """
    pid = start_pid
    for _ in range(_MAX_ANCESTRY_DEPTH):
        if pid <= 1:
            return None
        if is_claude_process(comm_of(pid), argv0_basename_of(pid)):
            return pid
        parent = ppid_of(pid)
        if parent is None or parent == pid:
            return None
        pid = parent
    return None
