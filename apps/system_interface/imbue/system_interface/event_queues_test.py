"""Tests for the agent event queues."""

import threading

from imbue.system_interface.event_queues import AgentEventQueues
from imbue.system_interface.event_queues import _EVENT_BUFFER_MAX_SIZE
from imbue.system_interface.event_queues import _EVENT_QUEUE_MAX_SIZE
from imbue.system_interface.event_queues import _MAX_CONSECUTIVE_QUEUE_FULL
from imbue.system_interface.events import BufferBehavior


def test_broadcast_delivers_to_registered_queue() -> None:
    queues = AgentEventQueues()
    q = queues.register("agent-1")
    queues.broadcast("agent-1", {"type": "test", "data": "hello"})
    event = q.get_nowait()
    assert event == {"type": "test", "data": "hello"}


def test_broadcast_does_not_deliver_to_other_agents() -> None:
    queues = AgentEventQueues()
    q1 = queues.register("agent-1")
    q2 = queues.register("agent-2")
    queues.broadcast("agent-1", {"type": "test"})
    assert not q2.empty() or q2.qsize() == 0
    assert q1.get_nowait() == {"type": "test"}
    assert q2.empty()


def test_unregister_removes_queue() -> None:
    queues = AgentEventQueues()
    q = queues.register("agent-1")
    queues.unregister("agent-1", q)
    queues.broadcast("agent-1", {"type": "test"})
    assert q.empty()


def test_buffer_replay_on_register() -> None:
    queues = AgentEventQueues()
    queues.broadcast("agent-1", {"type": "event-1"})
    queues.broadcast("agent-1", {"type": "event-2"})
    q = queues.register("agent-1")
    assert q.get_nowait() == {"type": "event-1"}
    assert q.get_nowait() == {"type": "event-2"}


def test_buffer_flush() -> None:
    queues = AgentEventQueues()
    queues.broadcast("agent-1", {"type": "event-1"})
    queues.broadcast("agent-1", {"type": "flush", "buffer_behavior": BufferBehavior.FLUSH})
    q = queues.register("agent-1")
    assert q.empty()


def test_buffer_ignore() -> None:
    queues = AgentEventQueues()
    queues.broadcast("agent-1", {"type": "event-1"})
    queues.broadcast("agent-1", {"type": "ephemeral", "buffer_behavior": BufferBehavior.IGNORE})
    q = queues.register("agent-1")
    assert q.get_nowait() == {"type": "event-1"}
    assert q.empty()


def test_buffer_behavior_stripped_from_delivered_events() -> None:
    queues = AgentEventQueues()
    q = queues.register("agent-1")
    queues.broadcast("agent-1", {"type": "test", "buffer_behavior": BufferBehavior.STORE})
    event = q.get_nowait()
    assert event is not None
    assert "buffer_behavior" not in event


def test_shutdown_sends_none_to_all() -> None:
    queues = AgentEventQueues()
    q1 = queues.register("agent-1")
    q2 = queues.register("agent-2")
    queues.shutdown()
    assert q1.get_nowait() is None
    assert q2.get_nowait() is None
    assert queues.is_shutdown


def test_register_after_shutdown_returns_closed_queue() -> None:
    queues = AgentEventQueues()
    queues.shutdown()
    q = queues.register("agent-1")
    assert q.get_nowait() is None


def test_registered_queue_is_bounded() -> None:
    """A subscriber's queue is created with a finite maxsize, not unbounded."""
    queues = AgentEventQueues()
    q = queues.register("agent-1")
    assert q.maxsize == _EVENT_QUEUE_MAX_SIZE


def test_slow_subscriber_is_evicted_after_sustained_overflow() -> None:
    """A subscriber that never drains is dropped and gets a None sentinel.

    The queue fills (it is never drained), and after
    ``_MAX_CONSECUTIVE_QUEUE_FULL`` further broadcasts the registry evicts the
    subscriber: it is removed from the fan-out set and its queue ends with a
    ``None`` terminator so the SSE generator closes and the client reconnects.
    """
    queues = AgentEventQueues()
    q = queues.register("agent-1")

    # Fill the queue to capacity. These are IGNORE so the replay buffer stays
    # empty and does not interfere with the per-subscriber overflow accounting.
    for i in range(_EVENT_QUEUE_MAX_SIZE):
        queues.broadcast("agent-1", {"seq": i, "buffer_behavior": BufferBehavior.IGNORE})
    assert q.full()

    # Subsequent broadcasts all hit queue.Full. The subscriber is evicted once
    # the consecutive-full counter reaches the threshold.
    for _ in range(_MAX_CONSECUTIVE_QUEUE_FULL):
        queues.broadcast("agent-1", {"overflow": True, "buffer_behavior": BufferBehavior.IGNORE})

    # The subscriber is no longer in the fan-out set: a fresh broadcast must
    # not raise and must not reach the evicted queue beyond its terminator.
    queues.broadcast("agent-1", {"after_eviction": True, "buffer_behavior": BufferBehavior.IGNORE})

    drained: list[object] = []
    while not q.empty():
        drained.append(q.get_nowait())
    assert drained[-1] is None, "evicted subscriber must receive a None terminator to close its stream"
    assert {"after_eviction": True} not in drained


def test_slow_subscriber_does_not_affect_healthy_one() -> None:
    """Evicting a stalled subscriber leaves a co-registered healthy one connected."""
    queues = AgentEventQueues()
    slow = queues.register("agent-1")
    healthy = queues.register("agent-1")

    total_broadcasts = _EVENT_QUEUE_MAX_SIZE + _MAX_CONSECUTIVE_QUEUE_FULL + 5
    for i in range(total_broadcasts):
        # The healthy subscriber drains every broadcast; the slow one never does.
        queues.broadcast("agent-1", {"seq": i, "buffer_behavior": BufferBehavior.IGNORE})
        healthy.get_nowait()

    # Healthy subscriber saw every event and is still registered (no terminator).
    assert healthy.empty()
    queues.broadcast("agent-1", {"final": True, "buffer_behavior": BufferBehavior.IGNORE})
    assert healthy.get_nowait() == {"final": True}

    # Slow subscriber was terminated: its backlog ends with a None sentinel.
    items: list[object] = []
    while not slow.empty():
        items.append(slow.get_nowait())
    assert items[-1] is None


def test_store_buffer_is_capped() -> None:
    """The replay buffer drops oldest events past the cap rather than growing forever."""
    queues = AgentEventQueues()
    overflow = 25
    for i in range(_EVENT_BUFFER_MAX_SIZE + overflow):
        queues.broadcast("agent-1", {"seq": i, "buffer_behavior": BufferBehavior.STORE})

    q = queues.register("agent-1")
    replayed: list[int] = []
    while not q.empty():
        event = q.get_nowait()
        assert event is not None
        replayed.append(event["seq"])

    assert len(replayed) == _EVENT_BUFFER_MAX_SIZE
    # Oldest events were dropped; the most recent window survives in order.
    assert replayed[0] == overflow
    assert replayed[-1] == _EVENT_BUFFER_MAX_SIZE + overflow - 1


def test_evict_closes_subscribers_and_drops_buffer() -> None:
    """evict() terminates every subscriber and clears the replay buffer for an agent."""
    queues = AgentEventQueues()
    queues.broadcast("agent-1", {"type": "stored", "buffer_behavior": BufferBehavior.STORE})
    q1 = queues.register("agent-1")
    q2 = queues.register("agent-1")
    # Drain the replayed STORE event so we can assert cleanly on the terminator.
    q1.get_nowait()
    q2.get_nowait()

    queues.evict("agent-1")

    assert q1.get_nowait() is None
    assert q2.get_nowait() is None

    # The replay buffer is gone: a freshly registered subscriber replays nothing.
    q3 = queues.register("agent-1")
    assert q3.empty()

    # A post-eviction broadcast does not reach the terminated subscribers.
    queues.broadcast("agent-1", {"type": "after", "buffer_behavior": BufferBehavior.IGNORE})
    assert q1.empty()
    assert q2.empty()


def test_register_tolerates_reentrant_unregister_from_same_thread() -> None:
    """register() runs arbitrary allocations inside its critical section
    (the put_nowait loop that replays buffered events). CPython can fire a
    GC cycle at any of those allocation points, and if GC finalizes an
    abandoned SSE event_generator the generator's `finally` block calls
    unregister() synchronously on the same thread. The registry's lock
    must be reentrant so that re-entrance does not self-deadlock.

    We simulate the re-entrance deterministically by installing a
    buffered-events list whose __iter__ calls unregister. If the lock is
    non-reentrant, register() deadlocks on itself and the wait times out.
    """
    queues = AgentEventQueues()
    existing_queue = queues.register("agent-1")

    class ReentrantOnIter(list[dict[str, object]]):
        def __iter__(self):
            queues.unregister("agent-1", existing_queue)
            return super().__iter__()

    queues._event_buffers["agent-1"] = ReentrantOnIter([{"type": "event"}])

    finished = threading.Event()

    def run_register() -> None:
        queues.register("agent-1")
        finished.set()

    worker = threading.Thread(target=run_register, daemon=True)
    worker.start()
    assert finished.wait(timeout=2.0), (
        "register() deadlocked; the lock must be reentrant so finalizers "
        "that call back into AgentEventQueues from the same thread succeed"
    )
