#!/usr/bin/env python3
"""Agent launch wrapper: tag this process's memory-shedding band, then exec claude.

Set as the claude agent type's ``command`` in ``.mngr/settings.toml`` so it
becomes the process mngr runs in the agent's tmux pane. It sets its *own*
``oom_score_adj`` to the user- or worker-agent band and records its pid in the
agent-pid registry, then ``exec``s the real ``claude`` with the exact arguments
mngr appended (``--settings``, ``--resume`` / ``--session-id``, etc.).

Because it execs in place, the band-tagged process *is* the claude process (same
pid -- ``oom_score_adj`` and the pid both survive ``execve``), so every
subprocess claude later spawns inherits the agent band by default; the PreToolUse
hook raises those subprocesses the rest of the way to the most-expendable band.
This replaces the old SessionStart hook that had to crawl up the process tree to
find the claude process after the fact -- here the process tags itself, so there
is nothing to discover.

The user-vs-worker band comes from the agent's label, resolved from the same
``MNGR_AGENT_NAME`` + host records the old hook used (see ``agent_identity``).

Tagging is best-effort: any failure (no writable ``/proc`` -- e.g. macOS -- or
host records that can't classify the agent) is swallowed so it can never block
the agent from starting. Exec is mandatory: if ``claude`` can't be launched the
failure propagates, since the agent cannot run without it.

Self-contained beyond the stdlib-only ``oom_priority`` package (imported via a
``sys.path`` insert), since this runs under a plain ``python3``.
"""

import os
import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "libs" / "oom_priority" / "src")
)

from oom_priority import bands
from oom_priority.agent_identity import is_worker_agent
from oom_priority.registry import record_agent_pid


def _tag_self() -> None:
    """Set this process's band and register its pid (so a later kill of it maps
    back to this agent). No-op when ``MNGR_AGENT_NAME`` is unset."""
    agent_name = os.environ.get("MNGR_AGENT_NAME", "")
    if not agent_name:
        return
    is_worker = is_worker_agent(agent_name)
    band = bands.WORKER_AGENT if is_worker else bands.USER_AGENT
    pid = os.getpid()
    bands.set_oom_score_adj(pid, band)
    record_agent_pid(pid, agent_name, is_worker)


def main() -> None:
    # Tag before exec so the band (and registry entry) are in place the instant
    # claude -- and any child it spawns -- exists. A tagging failure must never
    # stop the agent from launching: the band is an optimization, claude is not.
    try:
        _tag_self()
    except Exception as error:
        print(f"claude_oom_launch: tagging skipped: {error}", file=sys.stderr)
    os.execvp("claude", ["claude", *sys.argv[1:]])


if __name__ == "__main__":
    main()
