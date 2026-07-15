"""Memory-shedding priority bands and the helper that writes them.

Each process is assigned to one band by writing its ``oom_score_adj`` once at
startup. earlyoom reads ``/proc/<pid>/oom_score`` (the kernel badness, which
already folds in ``oom_score_adj``) to pick its victim, so a higher band makes a
process more likely to be shed first.

Bands are positive-only. A negative ``oom_score_adj`` (true "never kill") would
require ``CAP_SYS_RESOURCE``, which the container's default capability set does
not grant; positive values still establish the relative ordering. The never-kill
infrastructure (sshd, supervisord, earlyoom itself, tini, and tmux) keeps the
inherited default of 0 -- nothing needs to tag it -- and is additionally shielded
by earlyoom ``--avoid``. The supervisord services (the UI, the tunnel, the
terminal, the backups, ...) are tagged into the low ``SERVICE_BANDS`` range so
they stay well below the agent bands while remaining strictly ordered among
themselves.

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
# The workspace's primary (services) agent. Pinned to the same never-shed band as
# the infrastructure (``PROTECTED``): shedding it would tear down the workspace's
# supervised services and make it report a broken state, so it must outlive every
# other agent and service. Positive-only bands cannot express a true "never kill"
# (that needs ``CAP_SYS_RESOURCE``, which the container lacks), so this is the
# strongest available protection -- shed dead last, tied with sshd/supervisord.
PRIMARY_AGENT: Final[int] = PROTECTED
# A boundary marker, no longer assigned at launch: it equals ``CHAT_AGENT_FLOOR``
# (a maximally-engaged chat is as protected as a "user agent") and is the ceiling
# the service bands sit below. Every user-facing agent is a chat; there is no
# distinct plain-user-agent actor to tag with it.
USER_AGENT: Final[int] = 300
WORKER_AGENT: Final[int] = 600
AGENT_SUBPROCESS: Final[int] = 900

# Dynamic chat-agent band. A chat launches at ``CHAT_AGENT_BASE`` (the most
# expendable chat) and its expendability is re-tagged at runtime from live UI
# activity (see the system_interface ``ChatOomPrioritizer``): the more a chat is
# engaged with, the more protected it is, but every chat always stays strictly
# below ``WORKER_AGENT`` (workers are shed before any chat) and strictly above the
# service bands (a chat revives on its next message, so it is shed before a
# service). ``chat_agent_oom_score_adj`` maps the activity signals to a value in
# ``[CHAT_AGENT_FLOOR, CHAT_AGENT_BASE]``. Starting at the expendable end means a
# chat that is never re-tagged (dormant, or messaged outside the UI) stays
# maximally expendable rather than pinned to the protected floor.
CHAT_AGENT_BASE: Final[int] = 560  # idle chat; also the chat launch band
CHAT_AGENT_FLOOR: Final[int] = 300  # fully-engaged chat (most protected)
_CHAT_OPEN_BONUS: Final[int] = 80
_CHAT_VISIBLE_BONUS: Final[int] = 80
_CHAT_RECENCY_MAX_BONUS: Final[int] = 120
_CHAT_RECENCY_STEP: Final[int] = 15


def chat_agent_oom_score_adj(
    *, is_open: bool, is_visible: bool, recency_rank: int | None
) -> int:
    """Map a chat agent's live activity to its ``oom_score_adj``.

    Lower is more protected. Starting from ``CHAT_AGENT_BASE`` (a closed,
    stale chat), each engagement signal lowers the score:

    - ``is_open``: the chat has an open tab in the workspace UI.
    - ``is_visible``: the chat's tab is currently visible (implies open).
    - ``recency_rank``: this chat's position when the chats that have been
      messaged are sorted by last-message time, newest first (0 = most recently
      messaged). The bonus decays with rank, so more-recently-messaged chats are
      more protected than their peers. ``None`` means the chat has not been
      messaged (this session) and so gets no recency bonus -- a never-messaged
      chat must not be treated as if it were the most recent.

    The result is clamped to ``[CHAT_AGENT_FLOOR, CHAT_AGENT_BASE]`` so it always
    sits strictly between the service bands and ``WORKER_AGENT``.
    """
    recency_bonus = 0
    if recency_rank is not None:
        recency_bonus = max(
            0, _CHAT_RECENCY_MAX_BONUS - _CHAT_RECENCY_STEP * max(0, recency_rank)
        )
    adj = CHAT_AGENT_BASE
    if is_open:
        adj -= _CHAT_OPEN_BONUS
    if is_visible:
        adj -= _CHAT_VISIBLE_BONUS
    adj -= recency_bonus
    return max(CHAT_AGENT_FLOOR, min(CHAT_AGENT_BASE, adj))


# Supervisord service bands, keyed by the service key passed to
# ``scripts/oom_tag_service.py``. Every value sits strictly between PROTECTED (0)
# and USER_AGENT (300), so a service is *less* expendable than any agent (an
# agent's work revives on the next message, so it is shed first) but still
# steerable relative to the other services.
#
# The services are ordered from least- to most-expendable by how much losing one
# hurts: the terminal (raw shell access) and the UI come first, then the tunnel,
# then the runtime-state sync (github-sync, opt-in) and the host backup, then the
# app-watcher, then the placeholder ``web`` example. ``user`` is the single band
# every *user-created* service shares; it sits above every built-in service so a
# user's own service is shed before any built-in one, while staying below USER_AGENT.
#
# sshd and the other never-kill infrastructure (supervisord, earlyoom, tini,
# tmux) are deliberately absent: they keep the inherited PROTECTED default (0)
# and are additionally shielded by earlyoom ``--avoid``.
#
# This is a best-effort steer, not a hard guarantee. earlyoom picks the highest
# ``/proc/*/oom_score``, which folds each process's live memory usage in on top
# of ``oom_score_adj``, so a large enough memory gap between two services can
# still reorder adjacent bands. The order only decides which service goes when
# earlyoom is forced to shed inside the protected pool -- i.e. once everything
# more expendable (browsers, agent subprocesses, agents, user services) is gone.
USER_SERVICE: Final[int] = 200
SERVICE_BANDS: Final[dict[str, int]] = {
    "terminal": 10,
    "system_interface": 20,
    "cloudflared": 30,
    # Opt-in runtime/ sync (added by the github-sync skill); inherits the band the
    # now-removed runtime-backup service used to hold.
    "github-sync": 40,
    "host-backup": 50,
    "app-watcher": 60,
    "web": 70,
    "user": USER_SERVICE,
}

# The shared-browser band: the absolute ceiling, one above AGENT_SUBPROCESS, so a
# browser always outranks even an agent's build/test subprocess and is shed first.
# The browser program tags itself at spawn (inline in its supervisord command,
# which cannot import this module); the constant exists so the backstop listener
# can re-assert the value if that self-tag failed.
SHARED_BROWSER: Final[int] = 1000

# The floor of the browser band's *range*. Chromium deliberately overwrites any
# inherited ``oom_score_adj`` once per process at startup with its own internal
# gradation (browser/zygote 0, gpu/utility 200, renderers 300 -- see
# ``AdjustLinuxOOMScore`` in chromium's chrome_main_delegate.cc), which without
# correction would leave the memory-heavy renderers *more* protected than the
# agents whose work they serve. The kernel offers no way to forbid that lowering
# without ``CAP_SYS_RESOURCE``, so the browser service instead sweeps its process
# tree and remaps every self-lowered value into ``[SHARED_BROWSER_FLOOR,
# SHARED_BROWSER]`` via ``shared_browser_oom_score_adj``: the whole browser tree
# stays above AGENT_SUBPROCESS, while Chromium's relative ordering (which is worth
# keeping -- shedding one renderer kills one tab, not the whole browser) is
# preserved in compressed form. Chromium only writes each value once, so a
# remapped value sticks.
SHARED_BROWSER_FLOOR: Final[int] = 910


def shared_browser_oom_score_adj(self_assigned: int) -> int:
    """Map a value Chromium assigned within its own 0..1000 gradation into the
    browser band's range ``[SHARED_BROWSER_FLOOR, SHARED_BROWSER]``.

    Order-preserving (a more-expendable Chromium process stays more expendable)
    and clamped, so any input lands inside the band range -- in particular at or
    above the floor, which is what makes the browser service's sweep idempotent
    (it only remaps values *below* the floor).
    """
    clamped = max(0, min(1000, self_assigned))
    span = SHARED_BROWSER - SHARED_BROWSER_FLOOR
    return SHARED_BROWSER_FLOOR + round(clamped * span / 1000)

# Expected band per supervisord program whose *program name* is not a
# SERVICE_BANDS key, for the backstop listener (scripts/oom_tag_backstop.py).
# The OOM machinery itself (earlyoom, the listener) must stay PROTECTED -- it is
# what keeps every other band meaningful. deferred-install stays PROTECTED too:
# shedding the one-shot first-boot installer mid-run would leave provisioning
# half-done with no auto-restart to finish it.
_NON_SERVICE_PROGRAM_BANDS: Final[dict[str, int]] = {
    "earlyoom": PROTECTED,
    "oom-tag-backstop": PROTECTED,
    "deferred-install": PROTECTED,
    "browser": SHARED_BROWSER,
}


def supervisord_program_band(program_name: str) -> int:
    """The band a supervisord program is expected to occupy, by program name.

    A built-in service's program name doubles as its SERVICE_BANDS key; the
    handful of programs outside that map have explicit expected bands above.
    Anything unrecognized is a user-created service and falls back to
    ``USER_SERVICE``: an unknown process must default to being expendable, never
    to the protected default it would otherwise inherit.
    """
    if program_name in SERVICE_BANDS:
        return SERVICE_BANDS[program_name]
    return _NON_SERVICE_PROGRAM_BANDS.get(program_name, USER_SERVICE)


_PROC_DIR: Final[Path] = Path("/proc")


def _oom_score_adj_path(pid: int) -> Path:
    return _PROC_DIR / str(pid) / "oom_score_adj"


def read_oom_score_adj(pid: int) -> int | None:
    """Read ``pid``'s current ``oom_score_adj``, or None when it is unreadable
    (the process exited, or the host has no ``/proc`` -- e.g. macOS)."""
    try:
        return int(_oom_score_adj_path(pid).read_text().strip())
    except (OSError, ValueError):
        return None


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
