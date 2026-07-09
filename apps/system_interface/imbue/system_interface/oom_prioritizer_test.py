"""Unit tests for the chat OOM prioritizer.

The engine is exercised with injected collaborators: a fixed chat-id list, a pid
resolver backed by a dict, a monotonic fake clock, and a capturing ``set_adj`` so
the exact ``oom_score_adj`` written per pid is asserted against the band policy.
"""

from itertools import count

from oom_priority import bands

from imbue.system_interface.oom_prioritizer import ChatOomPrioritizer


class _Harness:
    """Wires a prioritizer to in-memory fakes and records every band write."""

    def __init__(self, chat_ids: list[str], pids: dict[str, int]) -> None:
        self.chat_ids = chat_ids
        self.pids = pids
        self.writes: list[tuple[int, int]] = []
        self._ticks = count(start=1)
        self.prioritizer = ChatOomPrioritizer(
            list_chat_agent_ids=lambda: list(self.chat_ids),
            resolve_pid=lambda cid: self.pids.get(cid),
            set_adj=self._set_adj,
            clock=lambda: float(next(self._ticks)),
        )

    def _set_adj(self, pid: int, adj: int) -> bool:
        self.writes.append((pid, adj))
        return True

    def latest_adj_by_pid(self) -> dict[int, int]:
        """The last band written per pid (a reapply rewrites all managed pids)."""
        result: dict[int, int] = {}
        for pid, adj in self.writes:
            result[pid] = adj
        return result


def test_open_and_visible_chat_is_more_protected_than_a_closed_one() -> None:
    h = _Harness(chat_ids=["a", "b"], pids={"a": 10, "b": 20})
    h.prioritizer.record_activity(open_ids=["a"], visible_ids=["a"], messaged_id=None)
    latest = h.latest_adj_by_pid()
    # ``a`` is open+visible; ``b`` is closed. Neither messaged, so no recency bonus.
    assert latest[10] == bands.chat_agent_oom_score_adj(is_open=True, is_visible=True, recency_rank=None)
    assert latest[20] == bands.chat_agent_oom_score_adj(is_open=False, is_visible=False, recency_rank=None)
    assert latest[10] < latest[20]


def test_more_recently_messaged_chat_ranks_more_protected() -> None:
    h = _Harness(chat_ids=["a", "b"], pids={"a": 10, "b": 20})
    # Message ``a`` first, then ``b``. Neither has an open tab, so only recency
    # differentiates them and ``b`` (newer) must end up more protected than ``a``.
    h.prioritizer.record_activity(open_ids=[], visible_ids=[], messaged_id="a")
    h.prioritizer.record_activity(open_ids=[], visible_ids=[], messaged_id="b")
    latest = h.latest_adj_by_pid()
    assert latest[20] < latest[10]
    assert latest[20] == bands.chat_agent_oom_score_adj(is_open=False, is_visible=False, recency_rank=0)
    assert latest[10] == bands.chat_agent_oom_score_adj(is_open=False, is_visible=False, recency_rank=1)


def test_visible_without_open_is_treated_as_open() -> None:
    h = _Harness(chat_ids=["a"], pids={"a": 10})
    # A report that lists ``a`` visible but not open (shouldn't happen, but be
    # defensive): visible implies open, so it scores as open+visible.
    h.prioritizer.record_activity(open_ids=[], visible_ids=["a"], messaged_id=None)
    assert h.latest_adj_by_pid()[10] == bands.chat_agent_oom_score_adj(
        is_open=True, is_visible=True, recency_rank=None
    )


def test_dormant_chat_without_a_live_pid_is_skipped() -> None:
    # ``b`` has no live pid, so it is skipped while ``a`` is tagged.
    h = _Harness(chat_ids=["a", "b"], pids={"a": 10})
    h.prioritizer.record_activity(open_ids=["a", "b"], visible_ids=[], messaged_id=None)
    assert set(h.latest_adj_by_pid()) == {10}


def test_revived_chat_is_tagged_on_the_next_reapply() -> None:
    # Dormant: no pid yet, so the first report tags nothing.
    h = _Harness(chat_ids=["a"], pids={})
    h.prioritizer.record_activity(open_ids=["a"], visible_ids=["a"], messaged_id="a")
    assert h.writes == []
    # The lifecycle poll finds the revived process and reapplies.
    h.pids["a"] = 10
    h.prioritizer.reapply()
    # ``a`` was messaged (rank 0), so it earns the recency bonus on top of open+visible.
    assert h.latest_adj_by_pid()[10] == bands.chat_agent_oom_score_adj(
        is_open=True, is_visible=True, recency_rank=0
    )


def test_non_chat_ids_in_the_report_are_ignored() -> None:
    # The frontend reports every tab; a worker/primary id that slips into the sets
    # must never be written, because it is not among the managed chat ids.
    h = _Harness(chat_ids=["chat"], pids={"chat": 10, "worker": 99})
    h.prioritizer.record_activity(open_ids=["chat", "worker"], visible_ids=["worker"], messaged_id="worker")
    assert set(h.latest_adj_by_pid()) == {10}


def test_later_report_replaces_presence_wholesale() -> None:
    h = _Harness(chat_ids=["a"], pids={"a": 10})
    h.prioritizer.record_activity(open_ids=["a"], visible_ids=["a"], messaged_id=None)
    protected = h.latest_adj_by_pid()[10]
    # The tab is closed; the next report drops it from both sets and ``a`` becomes
    # the most-expendable (base) chat again.
    h.prioritizer.record_activity(open_ids=[], visible_ids=[], messaged_id=None)
    reverted = h.latest_adj_by_pid()[10]
    assert reverted > protected
    assert reverted == bands.chat_agent_oom_score_adj(is_open=False, is_visible=False, recency_rank=None)
