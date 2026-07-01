"""The ONE sync<->async boundary for the whole browser library.

browser_use, Playwright (async API), and our per-browser ownership state machine
(session.py) are all asyncio-native and MUST run on an event loop. The Flask web
layer (runner.py) is synchronous, thread-per-connection. :class:`AsyncLoopBridge`
quarantines all the async behind ONE background event loop running on ONE
dedicated daemon thread; every Flask thread reaches the async world only through
``bridge.run(coro)`` / ``bridge.submit(coro)``, which push the coroutine onto
that loop via ``asyncio.run_coroutine_threadsafe`` -- the textbook, thread-safe
way to drive a loop owned by another thread.

Because every coroutine runs on this single loop thread, session.py's
``asyncio.Lock`` / ``asyncio.Event`` / ``asyncio.Task`` keep their meaning: the
state machine stays cooperatively single-threaded and its atomicity guarantees
are preserved unchanged. The preemptive OS threads (Flask workers) are preempted
only OUTSIDE the state machine, at the bridge call.
"""

import asyncio
import concurrent.futures
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

from loguru import logger

T = TypeVar("T")


class AsyncLoopBridge:
    """One background asyncio loop on one daemon thread, reached from sync code.

    Started once at Flask app construction (:meth:`start`) and stopped at
    shutdown (:meth:`stop`). The single primitive is
    ``asyncio.run_coroutine_threadsafe``.
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="browser-async-loop", daemon=True)
        self._ready = threading.Event()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self._loop

    def start(self) -> None:
        """Start the loop thread and block until the loop is actually running."""
        self._thread.start()
        self._ready.wait()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        # A fire-and-forget task that escapes its own error handling would otherwise
        # vanish into the loop's default handler; log it so a bug there is visible
        # rather than silently wedging the fleet.
        self._loop.set_exception_handler(self._on_loop_exception)
        self._loop.call_soon(self._ready.set)
        self._loop.run_forever()

    @staticmethod
    def _on_loop_exception(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        logger.error("unhandled exception on the browser async loop: {}", context.get("message", context))

    def _schedule(self, coro: Coroutine[Any, Any, T]) -> "asyncio.Task[T]":
        """Schedule ``coro`` on the loop and return its real asyncio.Task.

        Unlike ``run_coroutine_threadsafe`` (which hands back a
        ``concurrent.futures.Future`` with no way to reach the underlying task),
        this captures the actual ``asyncio.Task`` synchronously at schedule time,
        so ``loop.call_soon_threadsafe(task.cancel)`` is well-defined and can
        never miss the not-yet-started window. See :func:`cancel_task`.
        """
        handle: concurrent.futures.Future[asyncio.Task[T]] = concurrent.futures.Future()
        self._loop.call_soon_threadsafe(lambda: handle.set_result(self._loop.create_task(coro)))
        return handle.result()

    def run(self, coro: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
        """Run ``coro`` on the loop from a sync thread and block for its result.

        Blocks the calling Flask thread (correct under thread-per-connection) and
        re-raises whatever the coroutine raised, so existing error types
        (FleetFullError, KeyError, BrowserStartupError, PlaywrightError) propagate
        unchanged. On ``timeout`` the scheduled coroutine is CANCELLED on the loop
        before the ``TimeoutError`` propagates, so a partly-applied state mutation
        can't keep running after the route already returned an error.

        Pass ``timeout=None`` (the default) for the load-bearing acquire/hold
        paths, which legitimately block until the lease is granted or the client
        disconnects.
        """
        task = self._schedule(coro)
        future = asyncio.run_coroutine_threadsafe(_await_task(task), self._loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            cancel_task(self._loop, task)
            raise

    def submit(self, coro: Coroutine[Any, Any, T]) -> "asyncio.Task[T]":
        """Fire-and-forget ``coro`` onto the loop, returning its real asyncio.Task.

        The returned task is the in-loop handle the disconnect paths cancel via
        :func:`cancel_task`; it is captured synchronously at schedule time, so it
        is always present even if the client disconnects before the coroutine
        starts running.
        """
        return self._schedule(coro)

    def result(self, task: "asyncio.Task[T]", timeout: float | None = None) -> T:
        """Block the calling (sync) thread for the result of a task already on the loop.

        The mirror of :meth:`run` for the ``submit`` path: ``submit`` starts a coroutine
        and hands back its in-loop task (so the disconnect path can cancel it); ``result``
        later waits, from a Flask thread, for that same task's outcome -- so the web layer
        never needs its own ``async def`` just to ``await`` a task. On ``timeout`` the task
        is cancelled on the loop before ``TimeoutError`` propagates.
        """
        future = asyncio.run_coroutine_threadsafe(_await_task(task), self._loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            cancel_task(self._loop, task)
            raise

    def stop(self) -> None:
        """Stop the loop and join its thread (best-effort, bounded)."""
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)


async def _await_task(task: "asyncio.Task[T]") -> T:
    """Await a task that was scheduled on the loop, shielding the cancellation.

    ``run`` schedules the coroutine as a real ``asyncio.Task`` (so the disconnect
    path can cancel it) and then awaits it. If the *waiter* future is abandoned on
    timeout we still want the underlying task cancelled -- ``run`` does that
    explicitly via :func:`cancel_task`, and awaiting here simply propagates the
    task's result or exception.
    """
    return await task


def cancel_task(loop: asyncio.AbstractEventLoop, task: "asyncio.Task[Any]") -> None:
    """Cancel an asyncio.Task that lives on ``loop`` from any (sync) thread.

    Cancellation must be requested ON the loop thread; ``Task.cancel`` is not
    thread-safe to call from outside. This is the disconnect-as-lease primitive:
    when a streaming client drops, the Flask generator's ``finally`` calls this on
    the agent run's task so the run is actually cancelled (its in-loop ``finally``
    then re-enters the state machine and CAS-no-ops), not merely orphaned.
    """
    loop.call_soon_threadsafe(task.cancel)
