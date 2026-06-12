import json
import os
import subprocess
from pathlib import Path
from typing import Final

from loguru import logger

from memory_watchdog.data_types import MemoryPressure, ProcessInfo, TmuxPane

_PROC_DIR: Final[Path] = Path("/proc")
_MEMINFO_PATH: Final[Path] = _PROC_DIR / "meminfo"
_TMUX_PANE_FORMAT: Final[str] = "#{session_name}\t#{window_name}\t#{pane_pid}"
# Hard ceiling for the tmux query so a wedged tmux server can't stall a poll.
_TMUX_TIMEOUT_SECONDS: Final[float] = 5.0


def _read_process_info(pid: int) -> ProcessInfo | None:
    """Read one process's parent, RSS, and command line from /proc.

    Returns None if the process vanished mid-read (a normal race) or its
    /proc entry is unreadable.
    """
    status_path = _PROC_DIR / str(pid) / "status"
    cmdline_path = _PROC_DIR / str(pid) / "cmdline"
    try:
        status_text = status_path.read_text()
    except OSError:
        return None
    parent_pid = 0
    resident_kb = 0
    for line in status_text.splitlines():
        if line.startswith("PPid:"):
            parent_pid = int(line.split(":", 1)[1].strip())
        elif line.startswith("VmRSS:"):
            # Format: "VmRSS:\t   12345 kB"
            resident_kb = int(line.split(":", 1)[1].strip().split()[0])
    try:
        raw_cmdline = cmdline_path.read_bytes()
    except OSError:
        raw_cmdline = b""
    command_line = raw_cmdline.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    return ProcessInfo(
        pid=pid,
        parent_pid=parent_pid,
        resident_kb=resident_kb,
        command_line=command_line,
    )


def read_all_processes() -> list[ProcessInfo]:
    """Snapshot every process currently visible in /proc."""
    processes: list[ProcessInfo] = []
    for entry in _PROC_DIR.iterdir():
        if not entry.name.isdigit():
            continue
        process = _read_process_info(int(entry.name))
        if process is not None:
            processes.append(process)
    return processes


def read_tmux_panes() -> list[TmuxPane]:
    """List every tmux pane across all sessions as (session, window, pane_pid)."""
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", _TMUX_PANE_FORMAT],
            capture_output=True,
            text=True,
            timeout=_TMUX_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("Failed to list tmux panes: {}", e)
        return []
    if result.returncode != 0:
        logger.warning(
            "tmux list-panes exited {}: {}", result.returncode, result.stderr.strip()
        )
        return []
    panes: list[TmuxPane] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        session_name, window_name, pane_pid_str = parts
        if not pane_pid_str.isdigit():
            continue
        panes.append(
            TmuxPane(
                session_name=session_name,
                window_name=window_name,
                pane_pid=int(pane_pid_str),
            )
        )
    return panes


def read_memory_pressure() -> MemoryPressure:
    """Read MemTotal / MemAvailable from /proc/meminfo."""
    total_kb = 0
    available_kb = 0
    for line in _MEMINFO_PATH.read_text().splitlines():
        if line.startswith("MemTotal:"):
            total_kb = int(line.split(":", 1)[1].strip().split()[0])
        elif line.startswith("MemAvailable:"):
            available_kb = int(line.split(":", 1)[1].strip().split()[0])
    return MemoryPressure(total_kb=total_kb, available_kb=available_kb)


def read_agent_label_sets() -> tuple[frozenset[str], frozenset[str]]:
    """Scan the host's agent records for user-created vs agent-created names.

    Reads ``$MNGR_HOST_DIR/agents/*/data.json``; each record carries the agent
    ``name`` and a ``labels`` dict. An agent with ``user_created=true`` is
    user-created (tier 5); one with ``agent_created=true`` is a worker (tier 7).
    Returns (user_created_names, agent_created_names).
    """
    host_dir = os.environ.get("MNGR_HOST_DIR", "")
    if not host_dir:
        return frozenset(), frozenset()
    agents_dir = Path(host_dir) / "agents"
    if not agents_dir.is_dir():
        return frozenset(), frozenset()
    user_created: set[str] = set()
    agent_created: set[str] = set()
    for agent_dir in agents_dir.iterdir():
        data_path = agent_dir / "data.json"
        if not data_path.exists():
            continue
        try:
            data = json.loads(data_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to read agent record {}: {}", data_path, e)
            continue
        name = data.get("name")
        labels = data.get("labels")
        if not isinstance(name, str) or not isinstance(labels, dict):
            continue
        if str(labels.get("user_created", "")).lower() == "true":
            user_created.add(name)
        if str(labels.get("agent_created", "")).lower() == "true":
            agent_created.add(name)
    return frozenset(user_created), frozenset(agent_created)
