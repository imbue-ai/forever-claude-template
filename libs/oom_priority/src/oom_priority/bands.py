"""Memory-shedding priority bands and the helper that writes them.

Each process is assigned to one band by writing its ``oom_score_adj`` once at
startup. earlyoom reads ``/proc/<pid>/oom_score`` (the kernel badness, which
already folds in ``oom_score_adj``) to pick its victim, so a higher band makes a
process more likely to be shed first.

Bands are positive-only. A negative ``oom_score_adj`` (true "never kill") would
require ``CAP_SYS_RESOURCE``, which the container's default capability set does
not grant; positive values still establish the relative ordering. Protected
processes (the UI, the tunnel, the terminal, the backups, supervisord, sshd,
tmux, and earlyoom itself) simply keep the inherited default of 0 -- nothing
needs to tag them -- and are additionally shielded by earlyoom ``--avoid`` where
they have a distinct process name.

Raising a process's own (or a descendant's) ``oom_score_adj`` is unprivileged,
so the tagging hooks need no special capability.

This module is stdlib-only (see ``paths``): it is imported by the agent-tagging
and subprocess-tagging Claude hooks, which run under a plain ``python3``.
"""

from pathlib import Path
from typing import Final

# oom_score_adj value per band, most protected first. Tunable. Spaced so the
# ordering is unambiguous and there is room to interpose a band later.
PROTECTED: Final[int] = 0
USER_AGENT: Final[int] = 300
WORKER_AGENT: Final[int] = 600
AGENT_SUBPROCESS: Final[int] = 900

_PROC_DIR: Final[Path] = Path("/proc")


def _oom_score_adj_path(pid: int) -> Path:
    return _PROC_DIR / str(pid) / "oom_score_adj"


def set_oom_score_adj(pid: int, adj: int) -> bool:
    """Write ``pid``'s ``oom_score_adj`` to ``adj``. Returns whether it stuck.

    A failure (the process exited, or the value is rejected) is reported via the
    return value rather than raised: callers are best-effort hooks that must not
    break the thing they are tagging.
    """
    try:
        _oom_score_adj_path(pid).write_text(f"{adj}\n")
    except OSError:
        return False
    return True
