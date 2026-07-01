#!/usr/bin/env python3
"""earlyoom after-kill hook: record each shed process to the shed ledger.

earlyoom runs this (its ``-N`` script) once per kill, passing the victim's pid,
uid, and process name (comm) in the environment as ``EARLYOOM_PID`` /
``EARLYOOM_UID`` / ``EARLYOOM_NAME``. By the time it runs the process is already
gone, so we cannot inspect ``/proc`` for it -- instead we look the pid up in the
agent-pid registry to decide whether an agent's own main process was shed (which
later drives that agent's revival notice) or merely a subprocess.

Self-contained beyond the stdlib-only ``oom_priority`` package, which it imports
by putting that package's ``src`` on ``sys.path`` -- earlyoom invokes this under
a plain ``python3`` (the supervisord service environment), where ``uv`` and
third-party packages are not on hand.
"""

import os
import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "libs" / "oom_priority" / "src")
)

from oom_priority.ledger import append_shed_record
from oom_priority.registry import lookup_agent


def main() -> None:
    pid_text = os.environ.get("EARLYOOM_PID", "")
    if not pid_text.isdigit():
        return
    pid = int(pid_text)
    comm = os.environ.get("EARLYOOM_NAME", "")
    agent = lookup_agent(pid)
    if agent is not None:
        append_shed_record(
            pid=pid,
            comm=comm,
            agent_name=str(agent.get("agent_name")),
            is_worker=bool(agent.get("is_worker")),
        )
    else:
        append_shed_record(pid=pid, comm=comm, agent_name=None, is_worker=None)


if __name__ == "__main__":
    main()
