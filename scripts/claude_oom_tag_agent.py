#!/usr/bin/env python3
"""SessionStart hook: tag this agent's main process with its memory-shedding
priority band and register its pid.

Run once when a claude agent session starts. It walks up from itself to the
agent's ``claude`` process, sets that process's ``oom_score_adj`` to the user- or
worker-agent band (so earlyoom sheds worker agents before user agents, and both
only after their expendable subprocesses), and records the pid in the agent-pid
registry so a later kill of that exact process can be attributed back to this
agent (which drives the revival notice).

Tagging the claude process means every subprocess it later spawns inherits the
agent band by default; the PreToolUse hook raises those subprocesses the rest of
the way to the most-expendable band.

If the claude process can't be found, this no-ops: the agent keeps its inherited
``oom_score_adj`` of 0 (protected), which is the safe failure mode -- we would
rather not shed a user's agent than shed it on a bad guess.

Self-contained beyond the stdlib-only ``oom_priority`` package (imported via a
``sys.path`` insert), since claude runs SessionStart hooks under a plain
``python3``.
"""

import os
import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "libs" / "oom_priority" / "src")
)

from oom_priority import bands
from oom_priority.agent_identity import is_worker_agent
from oom_priority.process_tree import find_claude_ancestor
from oom_priority.registry import (
    current_process_ppid,
    read_argv0_basename,
    read_comm,
    record_agent_pid,
)


def main() -> None:
    agent_name = os.environ.get("MNGR_AGENT_NAME", "")
    if not agent_name:
        return
    claude_pid = find_claude_ancestor(
        os.getppid(),
        ppid_of=current_process_ppid,
        comm_of=read_comm,
        argv0_basename_of=read_argv0_basename,
    )
    if claude_pid is None:
        return
    is_worker = is_worker_agent(agent_name)
    band = bands.WORKER_AGENT if is_worker else bands.USER_AGENT
    bands.set_oom_score_adj(claude_pid, band)
    record_agent_pid(claude_pid, agent_name, is_worker)


if __name__ == "__main__":
    main()
