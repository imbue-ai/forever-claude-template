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
Because the process tags itself at launch, the band is set before any subprocess
exists -- the process that needs tagging is known directly, with no process tree
to inspect.

The user-vs-worker band comes from the agent's label, resolved from
``MNGR_AGENT_NAME`` + the host records (see ``agent_identity``).

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
from oom_priority.agent_identity import is_primary_agent, is_worker_agent
from oom_priority.registry import record_agent_pid


def _band_for(agent_name: str, *, is_worker: bool) -> int:
    """Pick the launch band for ``agent_name``.

    The primary (services) agent is pinned to the never-shed ``PRIMARY_AGENT``
    band; a worker to ``WORKER_AGENT``; everything else to ``USER_AGENT`` (the
    system_interface prioritizer later re-tags live chats within their own range,
    but this is the protected default until it does)."""
    if is_primary_agent(agent_name):
        return bands.PRIMARY_AGENT
    return bands.WORKER_AGENT if is_worker else bands.USER_AGENT


def _tag_self() -> None:
    """Set this process's band and register its pid (so a later kill of it maps
    back to this agent). No-op when ``MNGR_AGENT_NAME`` is unset."""
    agent_name = os.environ.get("MNGR_AGENT_NAME", "")
    if not agent_name:
        return
    is_worker = is_worker_agent(agent_name)
    band = _band_for(agent_name, is_worker=is_worker)
    pid = os.getpid()
    bands.set_oom_score_adj(pid, band)
    # Record the stable agent id too (when mngr exposes it) so the prioritizer can
    # resolve this pid by id to re-tag the chat at runtime.
    record_agent_pid(pid, agent_name, is_worker, agent_id=os.environ.get("MNGR_AGENT_ID") or None)


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
