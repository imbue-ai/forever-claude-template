"""Re-tag chat agents' memory-shedding priority from live workspace activity.

The launch wrapper tags every agent's ``oom_score_adj`` once at startup (see
``libs/oom_priority``). This engine keeps *chat* agents' scores current as the
user engages with them: the more a chat is engaged with, the more protected it is
from an out-of-memory shed. Three signals feed it, all reported by the frontend
through the single ``/api/activity`` endpoint:

- **open** -- the chat has a tab open in the workspace UI,
- **visible** -- the chat's tab is currently visible (implies open),
- **messaged** -- a message was just sent to the chat (drives a recency ranking
  across all chats, newest-first).

Only chat agents are managed. Workers and the primary (services) agent are
excluded by the caller's ``list_chat_agent_ids`` (they keep their launch bands --
workers stay maximally expendable, the primary stays pinned), so opening,
switching to, or messaging one of them never moves its score. A chat with no live
process (dormant, revives on its next message) is simply skipped until its
process exists; ``reapply`` is called both on every activity report and from the
agent manager's lifecycle poll so a revived chat lands on the right band despite
the launch race.

The band arithmetic lives in ``oom_priority.bands`` (the stdlib-only, testable
policy); this engine only holds the activity state and drives the writes. All
collaborators are injected so the engine is unit-testable without ``/proc``, the
agent manager, or the pid registry.
"""

import threading
import time
from collections.abc import Callable
from collections.abc import Iterable

from oom_priority import bands


class ChatOomPrioritizer:
    """Holds chat activity state and re-tags each chat's ``oom_score_adj``.

    ``list_chat_agent_ids`` returns the ids of the agents to manage (chats only;
    the caller excludes workers and the primary). ``resolve_pid`` maps a chat's
    agent id to its live main-process pid, or None when it has no running process.
    ``set_adj`` writes ``oom_score_adj`` for a pid (best-effort; its return value
    is ignored). ``clock`` supplies a monotonically increasing time for recency
    ordering (injectable for tests).
    """

    def __init__(
        self,
        *,
        list_chat_agent_ids: Callable[[], Iterable[str]],
        resolve_pid: Callable[[str], int | None],
        set_adj: Callable[[int, int], bool],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._list_chat_agent_ids = list_chat_agent_ids
        self._resolve_pid = resolve_pid
        self._set_adj = set_adj
        self._clock = clock
        self._lock = threading.Lock()
        self._open: set[str] = set()
        self._visible: set[str] = set()
        # agent_id -> monotonic time of its most recent message, for recency ranking.
        self._last_message_at: dict[str, float] = {}

    def record_activity(
        self,
        *,
        open_ids: Iterable[str],
        visible_ids: Iterable[str],
        messaged_id: str | None = None,
    ) -> None:
        """Apply a frontend activity report, then re-tag every chat.

        ``open_ids`` / ``visible_ids`` replace the tracked presence sets wholesale
        (idempotent and self-healing: a later report corrects any missed one).
        ``messaged_id`` -- when set -- stamps that chat as just-messaged so it
        ranks newest. The ids may include non-chat agents (the frontend reports
        every tab); they are ignored by ``reapply``, which only iterates the
        managed chats.
        """
        with self._lock:
            self._open = set(open_ids)
            self._visible = set(visible_ids)
            if messaged_id is not None:
                self._last_message_at[messaged_id] = self._clock()
        self.reapply()

    def reapply(self) -> None:
        """Recompute and write every managed chat's ``oom_score_adj``.

        Snapshots the activity state under the lock, then classifies, ranks by
        recency, resolves each chat's pid, and writes its band -- all outside the
        lock, so a write (or a call into the agent manager / pid registry) never
        blocks a concurrent activity report. Chats with no live process are
        skipped. Idempotent: concurrent reapplies converge on the same result.
        """
        with self._lock:
            open_ids = set(self._open)
            visible_ids = set(self._visible)
            last_message_at = dict(self._last_message_at)

        chat_ids = list(self._list_chat_agent_ids())

        # Rank the chats that have been messaged, newest first (rank 0 = most
        # recent). A chat never messaged this session gets no recency bonus -- it
        # ranks past the end of this list.
        messaged_newest_first = sorted(
            (cid for cid in chat_ids if cid in last_message_at),
            key=lambda cid: last_message_at[cid],
            reverse=True,
        )
        rank_by_id = {cid: rank for rank, cid in enumerate(messaged_newest_first)}
        no_recency_rank = len(messaged_newest_first)

        for chat_id in chat_ids:
            pid = self._resolve_pid(chat_id)
            if pid is None:
                continue
            # ``visible`` implies ``open`` even if a report omitted it from the open set.
            is_visible = chat_id in visible_ids
            is_open = is_visible or chat_id in open_ids
            adj = bands.chat_agent_oom_score_adj(
                is_open=is_open,
                is_visible=is_visible,
                recency_rank=rank_by_id.get(chat_id, no_recency_rank),
            )
            self._set_adj(pid, adj)
