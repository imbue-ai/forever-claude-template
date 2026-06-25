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

# Background services run as [program:*] children of supervisord (see
# supervisord.conf), not as their own tmux windows. A service is identified by a
# distinctive token in the command line of supervisord's direct child: for the
# `bash -c "... && realserver"` wrappers that token is the realserver name; for
# `uv run X` services it is X. The first matching token wins, so the tokens are
# chosen to be unambiguous across services. A supervisord child whose command
# matches nothing defaults to AUXILIARY_SERVICE (tier 6) -- so an agent-added
# program is shed before worker agents but after the recognized infrastructure.
_SERVICE_TIER_RULES: Final[tuple[tuple[str, str, Tier], ...]] = (
    ("system-interface", "system_interface", Tier.USER_INTERFACE),
    ("cloudflare-tunnel", "cloudflared", Tier.USER_INTERFACE),
    ("run_ttyd", "terminal", Tier.USER_INTERFACE),
    ("memory-watchdog", "memory-watchdog", Tier.RECOVERY),
    ("runtime-backup", "runtime-backup", Tier.DURABILITY),
    ("host-backup", "host-backup", Tier.DURABILITY),
    ("web-server", "web", Tier.AUXILIARY_SERVICE),
    ("app-watcher", "app-watcher", Tier.AUXILIARY_SERVICE),
    ("deferred_install", "deferred-install", Tier.AUXILIARY_SERVICE),
)

_SUPERVISORD_COMMAND_BASENAME: Final[str] = "supervisord"

# Depth (relative to an agent session's pane shell) at and below which a process
# is treated as an agent-spawned child (builds, tests, browsers) rather than the
# agent itself. The pane shell is depth 0, the claude process it launches is
# depth 1, and the tool subprocesses claude spawns are depth 2+.
_AGENT_CHILD_MIN_DEPTH: Final[int] = 2

# Per-agent coordination/observability helper processes that live inside an
# agent's subtree but are NOT expendable work. mngr runs a background-task loop
# for every agent, which spawns transcript streamers that feed the UI; a lead
# additionally runs a worker-report poll (create_worker.py await). These are tiny
# (KB-MB) yet load-bearing: shedding one frees nothing meaningful but blinds the
# UI or severs lead<->worker coordination (the lead's poll is what tells it a
# worker was paused). So they are classified as never-shed infrastructure rather
# than as expendable agent children, regardless of their depth. Matched by a
# distinctive token anywhere in the command line.
_AGENT_INFRA_HELPER_TOKENS: Final[tuple[str, ...]] = (
    "claude_background_tasks.sh",
    "stream_transcript.sh",
    "common_transcript.sh",
    "create_worker.py",
)


@pure
def _is_agent_infra_helper(command_line: str) -> bool:
    """Whether a process inside an agent's subtree is mngr coordination/observability
    machinery that must never be shed (see ``_AGENT_INFRA_HELPER_TOKENS``)."""
    return any(token in command_line for token in _AGENT_INFRA_HELPER_TOKENS)


@pure
def _command_basename(command_line: str) -> str:
    first_token = command_line.split(" ", 1)[0] if command_line else ""
    return first_token.rsplit("/", 1)[-1]


@pure
def _is_supervisord(command_line: str) -> bool:
    """Whether this process is supervisord (the root of the service subtree).

    supervisord may be exec'd directly (argv[0] = ``.../supervisord``) or run
    through the interpreter (``/usr/bin/python3 /usr/bin/supervisord ...``, which
    is how the container's image launches it). So match when the basename of
    either of the first two argv tokens is exactly ``supervisord``, rather than
    only checking argv[0] -- otherwise the interpreter form is missed and every
    service falls through to the protected infrastructure tier and is never shed.
    Only the first two tokens are considered so a later argument or config/log
    path (``supervisord.conf``) cannot be mistaken for the process itself.
    """
    tokens = command_line.split()
    return any(
        token.rsplit("/", 1)[-1] == _SUPERVISORD_COMMAND_BASENAME
        for token in tokens[:2]
    )


@pure
def _short_command_label(command_line: str, fallback: str) -> str:
    """A compact label for a process, for the ledger and banner."""
    basename = _command_basename(command_line)
    return basename or fallback


# Interpreters/launchers whose own name ("python3", "uv", "node") says little
# about what is actually running -- for these we look past the launcher to the
# first real target (a script path or subcommand) so the label is specific.
_COMMAND_RUNNERS: Final[frozenset[str]] = frozenset(
    {
        "python",
        "python3",
        "node",
        "nodejs",
        "bash",
        "sh",
        "uv",
        "uvx",
        "npx",
        "ruby",
        "perl",
        "env",
        "sudo",
    }
)
# Runner sub-tokens to skip while reaching for the real target (e.g. the "run"
# in "uv run pytest", the "-m" in "python3 -m pytest").
_RUNNER_SKIP_TOKENS: Final[frozenset[str]] = frozenset({"run", "exec", "-m"})


@pure
def _describe_command(command_line: str, fallback: str) -> str:
    """A specific label for a subprocess, e.g. "python3 hog.py", "pytest".

    A bare interpreter name ("python3") is uninformative, so when the command is
    a known runner we append the basename of the first real target token (the
    script or subcommand) -- "python3 /tmp/hog.py" -> "python3 hog.py",
    "uv run pytest" -> "uv pytest", "python3 -m pytest" -> "python3 pytest". A
    non-runner command keeps its own basename ("/usr/bin/pytest" -> "pytest").
    """
    tokens = command_line.split()
    if not tokens:
        return fallback or "process"
    runner = tokens[0].rsplit("/", 1)[-1]
    if runner not in _COMMAND_RUNNERS:
        return runner or fallback or "process"
    for token in tokens[1:]:
        if token in _RUNNER_SKIP_TOKENS:
            continue
        if token.startswith("-"):
            continue
        return f"{runner} {token.rsplit('/', 1)[-1]}"
    return runner


@pure
def _service_tier_and_label(command_line: str) -> tuple[Tier, str]:
    """Tier + label for one supervisord child, matched by its command line."""
    for token, label, tier in _SERVICE_TIER_RULES:
        if token in command_line:
            return tier, label
    return Tier.AUXILIARY_SERVICE, _short_command_label(command_line, "service")


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
def classify_processes(
    processes: Sequence[ProcessInfo],
    panes: Sequence[TmuxPane],
    services_session_name: str,
    mngr_prefix: str,
    user_created_agent_names: frozenset[str],
    agent_created_agent_names: frozenset[str],
) -> list[ProcessClassification]:
    """Assign every process an OOM-priority tier.

    Three passes:

    1. Services. Each supervisord child roots one service's subtree, tiered by
       matching its command line; supervisord itself is infrastructure. This is
       how background services are tiered now that they are supervisord children
       rather than individual ``svc-<name>`` tmux windows.
    2. Panes. Agent sessions map to their agent's tier, with that agent's tool
       subprocesses (depth >= 2) dropped to AGENT_CHILD. The services session's
       remaining processes -- the supervisord launch chain and the services
       agent's own idle shell -- are infrastructure we never shed.
    3. Leftovers. Processes under no pane (the tmux server, sshd, the container
       entrypoint, anything else) are treated as INFRASTRUCTURE.
    """
    process_by_pid: dict[int, ProcessInfo] = {p.pid: p for p in processes}
    children_by_parent = _build_children_by_parent(processes)

    classifications: list[ProcessClassification] = []
    assigned_pids: set[int] = set()

    # Pass 1: supervisord-managed services. supervisord (and its launch chain,
    # classified as infrastructure in pass 2) is never shed; each direct child
    # roots a service subtree tiered by command line.
    for process in processes:
        if not _is_supervisord(process.command_line):
            continue
        assigned_pids.add(process.pid)
        classifications.append(
            ProcessClassification(
                pid=process.pid,
                resident_kb=process.resident_kb,
                tier=Tier.INFRASTRUCTURE,
                label=_SUPERVISORD_COMMAND_BASENAME,
            )
        )
        for child_pid in children_by_parent.get(process.pid, ()):
            child = process_by_pid.get(child_pid)
            if child is None:
                continue
            tier, label = _service_tier_and_label(child.command_line)
            for pid, _depth in _walk_subtree_depths(
                child_pid, children_by_parent, frozenset(assigned_pids)
            ):
                descendant = process_by_pid.get(pid)
                if descendant is None:
                    continue
                assigned_pids.add(pid)
                classifications.append(
                    ProcessClassification(
                        pid=pid,
                        resident_kb=descendant.resident_kb,
                        tier=tier,
                        label=label,
                    )
                )

    # Pass 2: panes. Assign each pane's subtree; a process already claimed (by
    # pass 1 or an earlier pane) is not reclassified.
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
            owning_agent: str | None = None
            if is_services_session:
                # The supervisord launch chain (shell, uv) and the services
                # agent's own idle shell. Never shed: killing any of these tears
                # down supervisord or the services session itself.
                tier = Tier.INFRASTRUCTURE
                label = pane.window_name
            elif depth == 0:
                # An agent session's pane shell. Never shed it: killing it drops
                # the session instead of leaving it idle for revive-on-message.
                tier = Tier.INFRASTRUCTURE
                label = pane.session_name
            elif agent_name is not None:
                base_tier = _agent_tier(
                    agent_name, user_created_agent_names, agent_created_agent_names
                )
                owning_agent = agent_name
                if _is_agent_infra_helper(process.command_line):
                    # Per-agent coordination/observability machinery (the
                    # background-task loop, transcript streamers, a lead's
                    # worker-report poll). Never shed -- it frees ~nothing but
                    # blinds the UI or severs lead<->worker coordination -- so it
                    # rides with the protected infrastructure tier regardless of
                    # its depth in the agent's subtree.
                    tier = Tier.INFRASTRUCTURE
                    label = _describe_command(process.command_line, agent_name)
                elif depth >= _AGENT_CHILD_MIN_DEPTH:
                    tier = Tier.AGENT_CHILD
                    label = _describe_command(process.command_line, agent_name)
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
                    pid=pid,
                    resident_kb=process.resident_kb,
                    tier=tier,
                    label=label,
                    owning_agent_name=owning_agent,
                )
            )

    # Pass 3: everything outside any pane subtree is infrastructure we never shed.
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
