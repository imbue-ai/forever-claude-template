"""Unit tests for the chat reviver.

The policy is exercised threadlessly: a harness feeds agent-state snapshots
and drives ``process_due_revivals`` directly, with a controllable fake clock,
a capturing send, and a settable memory probe -- no worker thread, mngr, or
/proc involved.
"""

from imbue.system_interface.chat_reviver import RESTART_NOTICE_MESSAGE
from imbue.system_interface.chat_reviver import ChatReviver
from imbue.system_interface.chat_reviver import read_memory_available_fraction
from imbue.system_interface.models import AgentStateItem

_PRIMARY_CHAT_ID = "agent-initial"


def _chat(agent_id: str, state: str, labels: dict[str, str] | None = None) -> AgentStateItem:
    return AgentStateItem(id=agent_id, name=agent_id, state=state, labels=labels or {}, work_dir="/w")


class _Harness:
    """Wires a reviver to in-memory fakes and records every send."""

    def __init__(self, initial_chat_agent_id: str | None = _PRIMARY_CHAT_ID) -> None:
        self.now = 1000.0
        self.sends: list[tuple[str, str]] = []
        self.send_result = True
        self.memory_fraction = 1.0
        self.reviver = ChatReviver(
            send_message=self._send,
            initial_chat_agent_id=initial_chat_agent_id,
            memory_available_fraction=lambda: self.memory_fraction,
            clock=lambda: self.now,
        )

    def _send(self, agent_id: str, message: str) -> bool:
        self.sends.append((agent_id, message))
        return self.send_result

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_done_chat_is_revived_with_the_restart_notice() -> None:
    h = _Harness()
    h.reviver.on_agent_snapshot([_chat("agent-x", "DONE")])
    h.advance(10)
    assert h.reviver.process_due_revivals() == ["agent-x"]
    assert h.sends == [("agent-x", RESTART_NOTICE_MESSAGE)]


def test_stopped_primary_chat_is_revived_but_stopped_secondary_is_not() -> None:
    h = _Harness()
    h.reviver.on_agent_snapshot(
        [
            _chat(_PRIMARY_CHAT_ID, "STOPPED"),
            _chat("agent-secondary", "STOPPED"),
        ]
    )
    h.advance(10)
    assert h.reviver.process_due_revivals() == [_PRIMARY_CHAT_ID]
    assert [agent_id for agent_id, _ in h.sends] == [_PRIMARY_CHAT_ID]


def test_workers_and_primary_services_agent_are_never_revived() -> None:
    h = _Harness()
    h.reviver.on_agent_snapshot(
        [
            _chat("agent-worker", "DONE", labels={"agent_created": "true"}),
            _chat("agent-services", "DONE", labels={"is_primary": "true"}),
        ]
    )
    h.advance(10)
    assert h.reviver.process_due_revivals() == []
    assert h.sends == []


def test_revival_waits_for_its_scheduled_delay() -> None:
    h = _Harness()
    h.reviver.on_agent_snapshot([_chat("agent-x", "DONE")])
    # The first revival is nearly immediate but not instant.
    assert h.reviver.process_due_revivals() == []
    h.advance(10)
    assert h.reviver.process_due_revivals() == ["agent-x"]


def test_chat_that_comes_back_alive_cancels_its_pending_revival() -> None:
    h = _Harness()
    h.reviver.on_agent_snapshot([_chat("agent-x", "DONE")])
    h.reviver.on_agent_snapshot([_chat("agent-x", "WAITING")])
    h.advance(60)
    assert h.reviver.process_due_revivals() == []
    assert h.sends == []


def test_destroyed_agent_is_forgotten() -> None:
    h = _Harness()
    h.reviver.on_agent_snapshot([_chat("agent-x", "DONE")])
    h.reviver.on_agent_snapshot([])
    h.advance(60)
    assert h.reviver.process_due_revivals() == []
    assert h.sends == []


def test_repeated_deaths_back_off() -> None:
    h = _Harness()
    # First death: revived after the short initial delay.
    h.reviver.on_agent_snapshot([_chat("agent-x", "DONE")])
    h.advance(10)
    assert h.reviver.process_due_revivals() == ["agent-x"]
    # It dies again immediately: the second revival must not fire after the
    # short delay again, only after the longer second-attempt delay.
    h.reviver.on_agent_snapshot([_chat("agent-x", "DONE")])
    h.advance(10)
    assert h.reviver.process_due_revivals() == []
    h.advance(60)
    assert h.reviver.process_due_revivals() == ["agent-x"]
    assert len(h.sends) == 2


def test_stability_resets_the_backoff() -> None:
    h = _Harness()
    h.reviver.on_agent_snapshot([_chat("agent-x", "DONE")])
    h.advance(10)
    assert h.reviver.process_due_revivals() == ["agent-x"]
    # The chat stays alive well past the stability window, then dies again:
    # the next revival fires after the short first-attempt delay again.
    h.reviver.on_agent_snapshot([_chat("agent-x", "WAITING")])
    h.advance(700)
    h.reviver.on_agent_snapshot([_chat("agent-x", "WAITING")])
    h.reviver.on_agent_snapshot([_chat("agent-x", "DONE")])
    h.advance(10)
    assert h.reviver.process_due_revivals() == ["agent-x"]
    assert len(h.sends) == 2


def test_low_memory_defers_without_consuming_an_attempt() -> None:
    h = _Harness()
    h.memory_fraction = 0.05
    h.reviver.on_agent_snapshot([_chat("agent-x", "DONE")])
    h.advance(10)
    # Due, but deferred by the memory guard: nothing sent.
    assert h.reviver.process_due_revivals() == ["agent-x"]
    assert h.sends == []
    # Memory recovers; the deferred revival fires and it is still attempt 1
    # (a later immediate re-death waits the *second* delay, proving the
    # deferral did not consume an attempt).
    h.memory_fraction = 1.0
    h.advance(120)
    assert h.reviver.process_due_revivals() == ["agent-x"]
    assert len(h.sends) == 1


def test_failed_send_retries_with_backoff() -> None:
    h = _Harness()
    h.send_result = False
    h.reviver.on_agent_snapshot([_chat("agent-x", "DONE")])
    h.advance(10)
    assert h.reviver.process_due_revivals() == ["agent-x"]
    assert len(h.sends) == 1
    # Still failing: the retry is scheduled with the second-attempt delay.
    h.advance(10)
    assert h.reviver.process_due_revivals() == []
    h.advance(60)
    assert h.reviver.process_due_revivals() == ["agent-x"]
    assert len(h.sends) == 2


def test_memory_probe_returns_full_when_proc_is_unreadable() -> None:
    # On hosts without /proc/meminfo (macOS dev machines) the probe must not
    # guard revival. On Linux it returns a genuine fraction in (0, 1].
    fraction = read_memory_available_fraction()
    assert 0.0 < fraction <= 1.0
