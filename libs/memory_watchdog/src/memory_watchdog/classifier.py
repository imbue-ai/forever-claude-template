from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Final

from imbue.imbue_common.pure import pure

from memory_watchdog.data_types import (
    ProcessClassification,
    ProcessInfo,
    Tier,
    TmuxPane,
)

# Maps a bootstrap-managed service (the part of the window name after "svc-") to
# its tier. Services not listed here -- including any an agent adds to
# services.toml -- default to AUXILIARY_SERVICE (tier 6).
_TIER_BY_SERVICE_NAME: Final[dict[str, Tier]] = {
    "system_interface": Tier.USER_INTERFACE,
    "cloudflared": Tier.USER_INTERFACE,
    "memory-watchdog": Tier.RECOVERY,
    "runtime-backup": Tier.DURABILITY,
    "host-backup": Tier.DURABILITY,
    "web": Tier.AUXILIARY_SERVICE,
    "app-watcher": Tier.AUXILIARY_SERVICE,
    "deferred-install": Tier.AUXILIARY_SERVICE,
}

# Maps the non-service ("extra") windows of the services session to their tier.
# Window "0" is the services agent's idle sleep window -- protected so the
# session itself stays alive. Unlisted utility windows default to
# AUXILIARY_SERVICE.
_TIER_BY_SERVICES_WINDOW_NAME: Final[dict[str, Tier]] = {
    "0": Tier.INFRASTRUCTURE,
    "bootstrap": Tier.RECOVERY,
    "terminal": Tier.USER_INTERFACE,
    "telegram": Tier.AUXILIARY_SERVICE,
}

_SVC_PREFIX: Final[str] = "svc-"

# Depth (relative to an agent session's pane shell) at and below which a process
# is treated as an agent-spawned child (builds, tests, browsers) rather than the
# agent itself. The pane shell is depth 0, the claude process it launches is
# depth 1, and the tool subprocesses claude spawns are depth 2+.
_AGENT_CHILD_MIN_DEPTH: Final[int] = 2


@pure
def _tier_for_services_window(window_name: str) -> Tier:
    if window_name.startswith(_SVC_PREFIX):
        service_name = window_name[len(_SVC_PREFIX) :]
        return _TIER_BY_SERVICE_NAME.get(service_name, Tier.AUXILIARY_SERVICE)
    return _TIER_BY_SERVICES_WINDOW_NAME.get(window_name, Tier.AUXILIARY_SERVICE)


@pure
def _agent_name_from_session(session_name: str, mngr_prefix: str) -> str | None:
    """Return the agent name for an agent session, or None if it is not one.

    Agent sessions are named ``<mngr_prefix><agent_name>``. The services
    session is excluded by the caller, so any other prefixed session is an
    agent.
    """
    if mngr_prefix and session_name.startswith(mngr_prefix):
        return session_name[len(mngr_prefix) :]
    return None


@pure
def _agent_tier(
    agent_name: str,
    user_created_agent_names: frozenset[str],
    agent_created_agent_names: frozenset[str],
) -> Tier:
    """Tier for an agent session's main process.

    User-created agents are tier 5. Agents explicitly created by other agents
    (workers) are tier 7. An agent we have no label for defaults protectively to
    tier 5 -- we would rather shed it last than risk shedding a user's agent
    early.
    """
    if agent_name in user_created_agent_names:
        return Tier.USER_AGENT
    if agent_name in agent_created_agent_names:
        return Tier.WORKER_AGENT
    return Tier.USER_AGENT


@pure
def _build_children_by_parent(processes: Sequence[ProcessInfo]) -> dict[int, list[int]]:
    children_by_parent: dict[int, list[int]] = defaultdict(list)
    for process in processes:
        children_by_parent[process.parent_pid].append(process.pid)
    return children_by_parent


@pure
def _walk_subtree_depths(
    root_pid: int,
    children_by_parent: Mapping[int, Sequence[int]],
    already_assigned: frozenset[int],
) -> list[tuple[int, int]]:
    """Return (pid, depth) for every process in root_pid's subtree.

    The root is depth 0. Processes already assigned to another pane's subtree
    are skipped (and their descendants pruned) so each process lands in exactly
    one tier.
    """
    discovered: list[tuple[int, int]] = []
    frontier: list[tuple[int, int]] = [(root_pid, 0)]
    visited: set[int] = set()
    while frontier:
        pid, depth = frontier.pop()
        if pid in visited or pid in already_assigned:
            continue
        visited.add(pid)
        discovered.append((pid, depth))
        for child_pid in children_by_parent.get(pid, ()):
            frontier.append((child_pid, depth + 1))
    return discovered


@pure
def _short_command_label(command_line: str, fallback: str) -> str:
    """A compact label for a process, for the ledger and banner."""
    first_token = command_line.split(" ", 1)[0] if command_line else ""
    basename = first_token.rsplit("/", 1)[-1]
    return basename or fallback


@pure
def classify_processes(
    processes: Sequence[ProcessInfo],
    panes: Sequence[TmuxPane],
    services_session_name: str,
    mngr_prefix: str,
    user_created_agent_names: frozenset[str],
    agent_created_agent_names: frozenset[str],
) -> list[ProcessClassification]:
    """Assign every process an OOM-priority tier.

    Processes are grouped by the tmux pane whose subtree contains them. Panes in
    the services session map to a tier by window name; panes in an agent session
    map to the agent's tier, with that agent's tool subprocesses (depth >= 2)
    dropped to AGENT_CHILD. Processes under no pane (the tmux server, sshd, the
    container entrypoint, and anything else we do not recognize) are treated as
    INFRASTRUCTURE so they are never shed.
    """
    process_by_pid: dict[int, ProcessInfo] = {p.pid: p for p in processes}
    children_by_parent = _build_children_by_parent(processes)

    classifications: list[ProcessClassification] = []
    assigned_pids: set[int] = set()

    # Assign each pane's subtree. Panes are processed in order; a process already
    # claimed by an earlier pane is not reclassified.
    for pane in panes:
        if pane.pane_pid not in process_by_pid:
            continue
        is_services_session = pane.session_name == services_session_name
        agent_name = (
            None
            if is_services_session
            else _agent_name_from_session(pane.session_name, mngr_prefix)
        )

        subtree = _walk_subtree_depths(
            pane.pane_pid, children_by_parent, frozenset(assigned_pids)
        )
        for pid, depth in subtree:
            process = process_by_pid.get(pid)
            if process is None:
                continue
            if depth == 0:
                # The pane's own shell. Never shed it: killing it tears down the
                # window, which would lose terminal access, prevent bootstrap
                # from detecting a service exit (its exit-status recorder runs in
                # this shell), and drop an agent session instead of leaving it
                # idle for revive-on-message.
                tier = Tier.INFRASTRUCTURE
                label = pane.window_name if is_services_session else pane.session_name
            elif is_services_session:
                tier = _tier_for_services_window(pane.window_name)
                label = pane.window_name
            elif agent_name is not None:
                base_tier = _agent_tier(
                    agent_name, user_created_agent_names, agent_created_agent_names
                )
                if depth >= _AGENT_CHILD_MIN_DEPTH:
                    tier = Tier.AGENT_CHILD
                    label = _short_command_label(process.command_line, agent_name)
                else:
                    tier = base_tier
                    label = agent_name
            else:
                # A prefixed session we cannot interpret; protect it.
                tier = Tier.USER_AGENT
                label = pane.session_name
            assigned_pids.add(pid)
            classifications.append(
                ProcessClassification(
                    pid=pid, resident_kb=process.resident_kb, tier=tier, label=label
                )
            )

    # Everything outside any pane subtree is infrastructure we never shed.
    for process in processes:
        if process.pid in assigned_pids:
            continue
        classifications.append(
            ProcessClassification(
                pid=process.pid,
                resident_kb=process.resident_kb,
                tier=Tier.INFRASTRUCTURE,
                label=_short_command_label(process.command_line, "system"),
            )
        )

    return classifications
