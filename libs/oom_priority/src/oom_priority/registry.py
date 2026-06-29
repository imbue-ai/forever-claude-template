"""The agent-pid registry: maps an agent's main process pid to its identity.

earlyoom's after-kill hook is handed only the killed pid, uid, and process name
(comm) -- and by then the process is gone, so it can't inspect ``/proc`` to learn
which agent it was. An agent's main process is a ``claude`` process whose comm
("claude"/"node") does not reveal the agent name. So each agent records its own
main-process pid here at session start; the kill hook looks the killed pid up to
decide whether an *agent* was shed (which drives revival) versus a mere
subprocess.

One file per pid (``<pid>.json``) rather than a shared map, so concurrent agents
never race on a single file and no locking is needed. Stale entries (whose pid
has been reused or is simply gone) are pruned best-effort by writers.

Stdlib-only (see ``paths``): imported by Claude hooks under a plain ``python3``.
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


def record_agent_pid(pid: int, agent_name: str, is_worker: bool) -> None:
    """Register ``pid`` as the main process of ``agent_name``.

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
    payload = json.dumps({"agent_name": agent_name, "is_worker": is_worker})
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


def read_comm(pid: int) -> str:
    """``/proc/<pid>/comm`` (the process name, <=15 chars), or "" if unreadable."""
    try:
        return (_PROC_DIR / str(pid) / "comm").read_text().strip()
    except OSError:
        return ""


def read_argv0_basename(pid: int) -> str:
    """Basename of the program ``pid`` is running (argv[0] from cmdline), or ""."""
    try:
        raw = (_PROC_DIR / str(pid) / "cmdline").read_bytes()
    except OSError:
        return ""
    first = raw.split(b"\x00", 1)[0].decode("utf-8", "replace")
    return first.rsplit("/", 1)[-1]


def current_process_ppid(pid: int) -> int | None:
    """Parent pid of ``pid`` from ``/proc/<pid>/stat``, or None if unavailable.

    Used by the agent-tagging hook to walk up its own ancestry to the claude
    process. Parsed from the stat line's last ``)``-delimited field split so a
    process whose comm contains spaces or parentheses does not shift the columns.
    """
    try:
        stat = (_PROC_DIR / str(pid) / "stat").read_text()
    except OSError:
        return None
    try:
        after_comm = stat.rsplit(")", 1)[1].split()
        # Fields after "pid (comm)": state ppid ... -> ppid is index 1 here.
        return int(after_comm[1])
    except (IndexError, ValueError):
        return None
