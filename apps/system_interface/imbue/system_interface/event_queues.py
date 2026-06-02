import queue
import threading
from collections import defaultdict
from typing import Any
from typing import Final

from loguru import logger as _loguru_logger

from imbue.system_interface.events import BufferBehavior

# Per-subscriber queue depth. Holds at most this many undelivered events before
# the registry starts counting the subscriber as unresponsive. SSE consumers
# drain continuously in normal operation, so reaching this depth means the
# consumer (or its TCP connection) has stalled. Matches the depth the
# WebSocketBroadcaster uses for the same reason.
_EVENT_QUEUE_MAX_SIZE: Final[int] = 1000

# How many *consecutive* broadcasts a single subscriber can be ``queue.Full``
# for before it is evicted. A momentarily-slow consumer that drains even one
# event between broadcasts resets the counter and stays connected; only a
# consumer that makes zero progress over this many broadcasts is dropped.
_MAX_CONSECUTIVE_QUEUE_FULL: Final[int] = 50

# Hard cap on the per-agent replay buffer. The buffer only grows for
# ``BufferBehavior.STORE`` events (none are emitted today -- the live session
# watcher uses ``IGNORE`` -- but the default behavior is ``STORE``, so any
# future caller that omits ``buffer_behavior`` would otherwise grow it without
# bound for the agent's lifetime). When the cap is exceeded the oldest events
# are dropped; late-joining subscribers replay only the most recent window.
_EVENT_BUFFER_MAX_SIZE: Final[int] = 1000


def _drain_queue(event_queue: queue.Queue[dict[str, Any] | None]) -> None:
    """Remove all pending items from ``event_queue`` so it ends up empty."""
    is_drained = False
    while not is_drained:
        try:
            event_queue.get_nowait()
        except queue.Empty:
            is_drained = True


class AgentEventQueues:
    """Thread-safe registry of per-agent event queues.

    Adapted from llm-webchat's ConversationEventQueues but keyed by agent_id
    instead of conversation_id.

    Each subscriber gets a bounded queue. A subscriber that stops draining
    (a slow or half-dead SSE consumer) would otherwise be an unbounded memory
    leak, so once its queue stays full across ``_MAX_CONSECUTIVE_QUEUE_FULL``
    consecutive broadcasts it is evicted: its queue is drained and a ``None``
    sentinel is delivered, which tells the SSE generator to close. The browser
    EventSource then reconnects and refetches a fresh snapshot, so no events
    are silently lost.
    """

    def __init__(self) -> None:
        self._queues: dict[str, list[queue.Queue[dict[str, Any] | None]]] = defaultdict(list)
        self._event_buffers: dict[str, list[dict[str, Any]]] = {}
        # Consecutive-``queue.Full`` count per subscriber, keyed by ``id(queue)``
        # to avoid hashing the queue. Reset to 0 on any successful enqueue.
        self._consecutive_full_by_id: dict[int, int] = {}
        # Reentrant because a CPython GC cycle during a put_nowait call inside
        # the locked register() section can finalize an abandoned SSE
        # event_generator (from an unrelated prior stream), whose `finally`
        # block calls unregister() on the same thread. The class never calls
        # its own API directly -- the runtime effectively inserts the
        # unregister() call mid-register() via a GC finalizer. With a
        # non-reentrant Lock that indirect re-entrance self-deadlocks.
        self._lock: threading.RLock = threading.RLock()
        self._shutdown: bool = False

    @property
    def is_shutdown(self) -> bool:
        return self._shutdown

    def register(self, agent_id: str) -> queue.Queue[dict[str, Any] | None]:
        event_queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=_EVENT_QUEUE_MAX_SIZE)
        with self._lock:
            if self._shutdown:
                event_queue.put_nowait(None)
                return event_queue
            buffered_events = self._event_buffers.get(agent_id, [])
            for event in buffered_events:
                event_queue.put_nowait(event)
            self._queues[agent_id].append(event_queue)
            self._consecutive_full_by_id[id(event_queue)] = 0
        return event_queue

    def unregister(self, agent_id: str, event_queue: queue.Queue[dict[str, Any] | None]) -> None:
        with self._lock:
            self._consecutive_full_by_id.pop(id(event_queue), None)
            queues = self._queues.get(agent_id)
            if queues is not None:
                try:
                    queues.remove(event_queue)
                except ValueError:
                    pass
                if not queues:
                    del self._queues[agent_id]

    def broadcast(self, agent_id: str, event: dict[str, Any]) -> None:
        behavior = BufferBehavior(event.get("buffer_behavior", BufferBehavior.STORE))
        clean_event = {key: value for key, value in event.items() if key != "buffer_behavior"}
        with self._lock:
            if behavior is BufferBehavior.STORE:
                agent_buffer = self._event_buffers.setdefault(agent_id, [])
                agent_buffer.append(clean_event)
                if len(agent_buffer) > _EVENT_BUFFER_MAX_SIZE:
                    del agent_buffer[: len(agent_buffer) - _EVENT_BUFFER_MAX_SIZE]
            elif behavior is BufferBehavior.FLUSH:
                self._event_buffers.pop(agent_id, None)
            queues = list(self._queues.get(agent_id, []))
            dead_queues: list[queue.Queue[dict[str, Any] | None]] = []
            for event_queue in queues:
                try:
                    event_queue.put_nowait(clean_event)
                    self._consecutive_full_by_id[id(event_queue)] = 0
                except queue.Full:
                    new_count = self._consecutive_full_by_id.get(id(event_queue), 0) + 1
                    self._consecutive_full_by_id[id(event_queue)] = new_count
                    if new_count >= _MAX_CONSECUTIVE_QUEUE_FULL:
                        dead_queues.append(event_queue)
            for dead_queue in dead_queues:
                self._evict_subscriber_locked(agent_id, dead_queue)

    def _evict_subscriber_locked(
        self, agent_id: str, dead_queue: queue.Queue[dict[str, Any] | None]
    ) -> None:
        """Drop a stalled subscriber and signal it to close. Caller holds ``self._lock``."""
        self._consecutive_full_by_id.pop(id(dead_queue), None)
        queues = self._queues.get(agent_id)
        if queues is not None:
            try:
                queues.remove(dead_queue)
            except ValueError:
                pass
            if not queues:
                del self._queues[agent_id]
        _drain_queue(dead_queue)
        dead_queue.put_nowait(None)
        _loguru_logger.warning(
            "Evicted unresponsive SSE subscriber for agent {} after {} consecutive queue-full broadcasts",
            agent_id,
            _MAX_CONSECUTIVE_QUEUE_FULL,
        )

    def evict(self, agent_id: str) -> None:
        """Free all server-side state for an agent and close its subscribers.

        Called when an agent is destroyed. Drops the replay buffer and signals
        every registered subscriber to close by draining its queue and
        delivering a ``None`` sentinel.
        """
        with self._lock:
            self._event_buffers.pop(agent_id, None)
            queues = self._queues.pop(agent_id, None)
            if queues is None:
                return
            for event_queue in queues:
                self._consecutive_full_by_id.pop(id(event_queue), None)
                _drain_queue(event_queue)
                event_queue.put_nowait(None)

    def shutdown(self) -> None:
        with self._lock:
            self._shutdown = True
            for agent_queues in self._queues.values():
                for event_queue in agent_queues:
                    event_queue.put_nowait(None)
            self._queues.clear()
            self._event_buffers.clear()
            self._consecutive_full_by_id.clear()
