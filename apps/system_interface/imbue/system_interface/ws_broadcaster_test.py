"""Tests for the WebSocket broadcaster."""

import json
import queue

from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster
from imbue.system_interface.ws_broadcaster import _CLIENT_QUEUE_MAX_SIZE
from imbue.system_interface.ws_broadcaster import _MAX_CONSECUTIVE_QUEUE_FULL

# A stuck client must hit ``queue.Full`` ``_MAX_CONSECUTIVE_QUEUE_FULL`` times
# before the broadcaster evicts it. The first ``_CLIENT_QUEUE_MAX_SIZE``
# broadcasts fill the queue without overflow; broadcasts after that overflow.
_BROADCASTS_TO_TRIGGER_DISCONNECT = _CLIENT_QUEUE_MAX_SIZE + _MAX_CONSECUTIVE_QUEUE_FULL


def _get_message(q: queue.Queue[str | None]) -> str:
    """Get a non-None message from the queue."""
    value = q.get_nowait()
    assert value is not None
    return value


def test_register_returns_queue() -> None:
    broadcaster = WebSocketBroadcaster()
    q = broadcaster.register()
    assert isinstance(q, queue.Queue)


def test_broadcast_puts_message_in_all_queues() -> None:
    broadcaster = WebSocketBroadcaster()
    q1 = broadcaster.register()
    q2 = broadcaster.register()

    broadcaster.broadcast({"type": "test", "data": 42})

    msg1 = json.loads(_get_message(q1))
    msg2 = json.loads(_get_message(q2))
    assert msg1 == {"type": "test", "data": 42}
    assert msg2 == {"type": "test", "data": 42}


def test_unregister_removes_queue() -> None:
    broadcaster = WebSocketBroadcaster()
    q = broadcaster.register()
    broadcaster.unregister(q)

    broadcaster.broadcast({"type": "test"})
    assert q.empty()


def test_unregister_nonexistent_is_safe() -> None:
    broadcaster = WebSocketBroadcaster()
    other_queue: queue.Queue[str | None] = queue.Queue()
    broadcaster.unregister(other_queue)


def test_broadcast_agents_updated() -> None:
    broadcaster = WebSocketBroadcaster()
    q = broadcaster.register()

    agents = [{"id": "a1", "name": "agent-1", "state": "RUNNING"}]
    broadcaster.broadcast_agents_updated(agents)

    msg = json.loads(_get_message(q))
    assert msg["type"] == "agents_updated"
    assert msg["agents"] == agents


def test_broadcast_applications_updated() -> None:
    broadcaster = WebSocketBroadcaster()
    q = broadcaster.register()

    apps = [{"name": "web", "url": "http://localhost:8000"}]
    broadcaster.broadcast_applications_updated(apps)

    msg = json.loads(_get_message(q))
    assert msg["type"] == "applications_updated"
    assert msg["applications"] == apps


def test_broadcast_proto_agent_created() -> None:
    broadcaster = WebSocketBroadcaster()
    q = broadcaster.register()

    broadcaster.broadcast_proto_agent_created(
        agent_id="a1", name="test", creation_type="worktree", parent_agent_id=None
    )

    msg = json.loads(_get_message(q))
    assert msg["type"] == "proto_agent_created"
    assert msg["agent_id"] == "a1"
    assert msg["creation_type"] == "worktree"
    assert msg["parent_agent_id"] is None


def test_broadcast_proto_agent_completed() -> None:
    broadcaster = WebSocketBroadcaster()
    q = broadcaster.register()

    broadcaster.broadcast_proto_agent_completed(agent_id="a1", success=True, error=None)

    msg = json.loads(_get_message(q))
    assert msg["type"] == "proto_agent_completed"
    assert msg["success"] is True
    assert msg["error"] is None


def test_broadcast_layout_op_open() -> None:
    broadcaster = WebSocketBroadcaster()
    q = broadcaster.register()

    broadcaster.broadcast_layout_op("open", {"ref": "service:web"}, requester_agent_id="agent-1")

    msg = json.loads(_get_message(q))
    assert msg == {
        "type": "layout_op",
        "op": "open",
        "args": {"ref": "service:web"},
        "requester_agent_id": "agent-1",
    }


def test_broadcast_layout_op_passes_args_through_unchanged() -> None:
    """Each op gets a distinct ``args`` payload; the broadcaster should not interpret it."""
    broadcaster = WebSocketBroadcaster()
    q = broadcaster.register()

    payload = {"ref": "service:web", "relative_to": "chat:alice", "direction": "right", "ratio": 0.5}
    broadcaster.broadcast_layout_op("split", payload, requester_agent_id="agent-2")

    msg = json.loads(_get_message(q))
    assert msg == {
        "type": "layout_op",
        "op": "split",
        "args": payload,
        "requester_agent_id": "agent-2",
    }


def test_broadcast_layout_op_defaults_requester_agent_id_to_empty_string() -> None:
    """``requester_agent_id`` is optional; omitting it yields an empty string."""
    broadcaster = WebSocketBroadcaster()
    q = broadcaster.register()

    broadcaster.broadcast_layout_op("restore", {})

    msg = json.loads(_get_message(q))
    assert msg["requester_agent_id"] == ""


def test_shutdown_sends_none_sentinel() -> None:
    broadcaster = WebSocketBroadcaster()
    q = broadcaster.register()

    broadcaster.shutdown()

    assert q.get_nowait() is None


def test_broadcast_disconnects_client_after_consecutive_queue_full_threshold() -> None:
    """A client whose queue stays full for the threshold's worth of broadcasts is disconnected."""
    broadcaster = WebSocketBroadcaster()
    stuck_queue = broadcaster.register()
    live_queue = broadcaster.register()

    # Push enough broadcasts to fill the stuck queue and then overflow it
    # ``_MAX_CONSECUTIVE_QUEUE_FULL`` times without the stuck client draining
    # anything. The live client drains as it goes (mimicking a healthy WS
    # handler) so only the stuck queue ever overflows.
    received_by_live_client: list[dict[str, int]] = []
    for index in range(_BROADCASTS_TO_TRIGGER_DISCONNECT):
        broadcaster.broadcast({"index": index})
        received_by_live_client.append(json.loads(_get_message(live_queue)))

    # Eviction drains the stuck queue and pushes the shutdown sentinel so the
    # client's handler thread (blocked on ``get``) wakes and exits. After
    # consuming that one sentinel the queue is empty and removed from the
    # roster, so a later broadcast must not touch it.
    assert stuck_queue.get_nowait() is None
    assert stuck_queue.empty()
    broadcaster.broadcast({"after": "evict"})
    assert stuck_queue.empty()

    # The live client got every broadcast -- the eviction did not interrupt it.
    assert len(received_by_live_client) == _BROADCASTS_TO_TRIGGER_DISCONNECT
    assert received_by_live_client[-1] == {"index": _BROADCASTS_TO_TRIGGER_DISCONNECT - 1}


def test_broadcast_does_not_disconnect_below_consecutive_threshold() -> None:
    """A client whose queue is full for fewer broadcasts than the threshold must NOT be disconnected."""
    broadcaster = WebSocketBroadcaster()
    stuck_queue = broadcaster.register()

    # Fill the queue, then overflow exactly one fewer time than the threshold.
    overflow_count_short_of_threshold = _MAX_CONSECUTIVE_QUEUE_FULL - 1
    for index in range(_CLIENT_QUEUE_MAX_SIZE + overflow_count_short_of_threshold):
        broadcaster.broadcast({"index": index})

    # No sentinel yet -- the client is still considered alive. The queue is at
    # capacity with the original (oldest) ``_CLIENT_QUEUE_MAX_SIZE`` messages.
    drained: list[str | None] = []
    while not stuck_queue.empty():
        drained.append(stuck_queue.get_nowait())
    assert None not in drained
    assert len(drained) == _CLIENT_QUEUE_MAX_SIZE


def test_broadcast_resets_overflow_count_after_successful_enqueue() -> None:
    """A briefly-stalled client that drains a message resets the overflow counter."""
    broadcaster = WebSocketBroadcaster()
    stuck_queue = broadcaster.register()

    # Fill the queue then overflow one short of the threshold.
    for index in range(_CLIENT_QUEUE_MAX_SIZE + (_MAX_CONSECUTIVE_QUEUE_FULL - 1)):
        broadcaster.broadcast({"index": index})

    # Client drains a single message, simulating recovery from a stall.
    stuck_queue.get_nowait()

    # The next broadcast succeeds (queue had room) and resets the counter to 0.
    broadcaster.broadcast({"recovered": True})

    # Now overflow ``_MAX_CONSECUTIVE_QUEUE_FULL - 1`` more times -- still below
    # threshold from the post-reset baseline. The client should remain connected.
    for index in range(_MAX_CONSECUTIVE_QUEUE_FULL - 1):
        broadcaster.broadcast({"after_reset_index": index})

    # Drain everything; no sentinel should be present.
    drained: list[str | None] = []
    while not stuck_queue.empty():
        drained.append(stuck_queue.get_nowait())
    assert None not in drained


def test_broadcast_after_disconnect_does_not_touch_dead_queue() -> None:
    """Once a stuck client is disconnected, further broadcasts skip its queue entirely."""
    broadcaster = WebSocketBroadcaster()
    stuck_queue = broadcaster.register()

    for index in range(_BROADCASTS_TO_TRIGGER_DISCONNECT):
        broadcaster.broadcast({"index": index})

    # The eviction path drains the queue and leaves only the shutdown sentinel;
    # subsequent broadcasts must not touch it.
    assert stuck_queue.get_nowait() is None
    assert stuck_queue.empty()

    broadcaster.broadcast({"after": "disconnect"})
    assert stuck_queue.empty()


def test_broadcast_warns_once_per_disconnect_not_per_dropped_message(
    loguru_records: list[str],
) -> None:
    """The flood-prevention fix: at most one warning per stuck client, not per drop."""
    broadcaster = WebSocketBroadcaster()
    broadcaster.register()

    # Filling and then over-pushing many times: a single eviction warning fires
    # at the threshold; later broadcasts have no client at all (the queue was
    # removed) so nothing additional is logged.
    for index in range(_BROADCASTS_TO_TRIGGER_DISCONNECT * 2):
        broadcaster.broadcast({"index": index})

    queue_full_warnings = [r for r in loguru_records if "Disconnected unresponsive" in r]
    assert len(queue_full_warnings) == 1


def test_broadcast_disconnect_unregisters_queue_so_unregister_is_idempotent() -> None:
    """After the broadcaster evicts a stuck client, the WS handler's later unregister is a noop."""
    broadcaster = WebSocketBroadcaster()
    stuck_queue = broadcaster.register()

    for index in range(_BROADCASTS_TO_TRIGGER_DISCONNECT):
        broadcaster.broadcast({"index": index})

    # Calling unregister (which the WS handler's finally does) must not raise even
    # though the broadcaster already removed the queue when it evicted the client.
    broadcaster.unregister(stuck_queue)
    broadcaster.unregister(stuck_queue)


def test_evicted_client_receives_shutdown_sentinel() -> None:
    """Eviction pushes the None sentinel so the blocked handler thread unblocks and exits."""
    broadcaster = WebSocketBroadcaster()
    stuck_queue = broadcaster.register()

    for index in range(_BROADCASTS_TO_TRIGGER_DISCONNECT):
        broadcaster.broadcast({"index": index})

    # The handler thread, blocked on ``get``, would receive this sentinel and
    # break out of its loop -- the thread-based replacement for task cancel.
    assert stuck_queue.get_nowait() is None


def test_shutdown_delivers_sentinel_even_to_full_queue() -> None:
    """Shutdown must signal even clients whose queues happen to be full."""
    broadcaster = WebSocketBroadcaster()
    stuck_queue = broadcaster.register()
    for index in range(_CLIENT_QUEUE_MAX_SIZE):
        # Bypass the broadcaster's full-handling so we can prepopulate the queue
        # exactly to capacity without triggering the disconnect path.
        stuck_queue.put_nowait(json.dumps({"index": index}))

    broadcaster.shutdown()

    # Drain everything; the very last value must be the None sentinel.
    drained: list[str | None] = []
    while not stuck_queue.empty():
        drained.append(stuck_queue.get_nowait())
    assert drained[-1] is None
