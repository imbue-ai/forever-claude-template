"""The agent-pid registry: maps an agent's main process pid to its identity.

earlyoom's after-kill hook is handed only the killed pid, uid, and process name
(comm) -- and by then the process is gone, so it can't inspect ``/proc`` to learn
which agent it was. An agent's main process is a ``claude`` process whose comm
("claude"/"node") does not reveal the agent name. So each agent records its own
main-process pid here at launch (the launch wrapper, ``scripts/claude_oom_launch.py``,
calls ``record_agent_pid`` for its own pid just before it execs claude); the kill
hook looks the killed pid up to decide whether an *agent* was shed (which drives
revival) versus a mere subprocess.

One file per pid (``<pid>.json``) rather than a shared map, so concurrent agents
never race on a single file and no locking is needed. Stale entries (whose pid
has been reused or is simply gone) are pruned best-effort by writers.

Stdlib-only (see ``paths``): imported by the launch wrapper and the kill hook
under a plain ``python3``.
"""

import json
from collections.abc import Callable
from pathlib import Path

from oom_priority.paths import agent_pids_dir

_PROC_DIR = Path("/proc")


def _entry_path(pid: int) -> Path:
    return agent_pids_dir() / f"{pid}.json"


def is_process_alive(pid: int) -> bool:
    """Whether ``pid`` is currently a live process (via ``/proc``)."""
    return (_PROC_DIR / str(pid)).exists()


def record_agent_pid(pid: int, agent_name: str, is_worker: bool, agent_id: str | None = None) -> None:
    """Register ``pid`` as the main process of ``agent_name``.

    ``agent_id`` (mngr's stable per-agent id) is recorded alongside the name so a
    consumer that knows only the id -- e.g. the system_interface OOM prioritizer,
    which re-tags a chat by id -- can resolve the live pid via
    ``lookup_pid_by_agent_id``. It is optional so older callers/tests that pass
    only a name still work.

    Overwrites any prior entry for the same pid (a reused pid), and prunes
    entries whose process no longer exists so the directory does not grow without
    bound across the container's life.
    """
    directory = agent_pids_dir()
    directory.mkdir(parents=True, exist_ok=True)
    # Prune BEFORE writing so a stale entry can never be mistaken for clearing
    # the entry we are about to add (the caller's own pid is live, so it is never
    # the one pruned).
    prune_dead_pids()
    record: dict[str, object] = {"agent_name": agent_name, "is_worker": is_worker}
    if agent_id is not None:
        record["agent_id"] = agent_id
    payload = json.dumps(record)
    _entry_path(pid).write_text(payload)


def lookup_agent(pid: int) -> dict | None:
    """Return ``{"agent_name", "is_worker"}`` for ``pid``, or None if not an
    agent main process (or the entry is missing/unreadable)."""
    try:
        data = json.loads(_entry_path(pid).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or "agent_name" not in data:
        return None
    return data


def lookup_pid_by_agent_id(agent_id: str, is_alive: Callable[[int], bool] = is_process_alive) -> int | None:
    """Return the live main-process pid recorded for ``agent_id``, or None.

    Scans the registry for an entry whose ``agent_id`` matches and whose pid is
    still a running process, so a consumer holding only the id (the OOM
    prioritizer) can re-tag that agent's ``oom_score_adj``. Returns None when no
    live entry matches -- e.g. a dormant chat with no running process, an id
    recorded before ``agent_id`` was captured, or a stale entry whose pid has
    exited. ``is_alive`` is injectable for testing without a real process tree.
    """
    directory = agent_pids_dir()
    if not directory.is_dir():
        return None
    for entry in directory.iterdir():
        if entry.suffix != ".json" or not entry.stem.isdigit():
            continue
        try:
            data = json.loads(entry.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or data.get("agent_id") != agent_id:
            continue
        pid = int(entry.stem)
        if is_alive(pid):
            return pid
    return None


def prune_dead_pids(is_alive: Callable[[int], bool] = is_process_alive) -> None:
    """Remove registry entries whose pid is no longer a live process.

    ``is_alive`` is injectable so the prune can be tested without a real process
    tree (the default consults ``/proc``).
    """
    directory = agent_pids_dir()
    if not directory.is_dir():
        return
    for entry in directory.iterdir():
        if entry.suffix != ".json" or not entry.stem.isdigit():
            continue
        if not is_alive(int(entry.stem)):
            try:
                entry.unlink()
            except OSError:
                pass
