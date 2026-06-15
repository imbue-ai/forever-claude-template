"""Tail an agent's response-streaming buffer and push in-progress assistant text.

mngr's Claude plugin can run a background watcher that captures the agent's tmux
pane on a fixed interval, reverse-maps the rendered assistant text back to
markdown, and writes the current in-progress message to
``$MNGR_AGENT_STATE_DIR/plugin/claude/stream_buffer`` (enabled by
``agent_types.claude.streaming_snapshot_interval_seconds > 0`` in the mngr
settings). The buffer format is:

    line 1     -> uuid of the last *complete* assistant message ("" if none)
    lines 2..  -> the in-progress assistant text, reverse-mapped to markdown

When the agent goes idle the body is emptied and only the id line remains.

This watcher tails that file and broadcasts an ``assistant_streaming`` snapshot
whenever the (last_complete_id, body) pair changes, so the chat view can render
a provisional bubble for the response as it is being typed. The real, durable
``assistant_message`` event still arrives through the session watcher when the
turn finalizes and supersedes the provisional bubble -- this stream is purely a
live preview, never persisted (it ships with IGNORE buffering, like the tickets
enrichment snapshot).

Polling, not watchdog
---------------------
Unlike the session and tickets watchers, this one does NOT hook watchdog. The
buffer is rewritten by mngr every ``streaming_snapshot_interval_seconds``; a
watchdog observer would wake this loop on every one of those writes, defeating
any rate limiting. A plain fixed-interval poll caps how often we read the file
and fan out to SSE clients regardless of how fast mngr writes, which keeps the
CPU cost predictable. The interval is deliberately conservative (see
``STREAM_POLL_INTERVAL_SECONDS``); lower it only if responses feel laggy. While
the agent is idle the buffer content is constant, so the change check below
emits nothing -- an idle agent costs one ``stat``+read per interval and no
broadcasts.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any
from typing import Callable

from loguru import logger as _loguru_logger

logger = _loguru_logger

# The SSE message type carrying an in-progress assistant response. The frontend
# renders a single provisional bubble from the latest one and clears it when the
# text goes empty or the finalized assistant_message arrives.
STREAMING_MESSAGE_TYPE = "assistant_streaming"

# How often to re-read the stream buffer. Conservative on purpose: the buffer is
# only a live preview that is superseded by the durable transcript event, and a
# tighter interval mostly just adds SSE fan-out churn. This is one of the two
# streaming intervals; the other is mngr's streaming_snapshot_interval_seconds
# (the tmux-capture cadence). The two loops are independent, so average
# end-to-end preview latency is roughly the sum of their half-intervals --
# pairing this 5s read with mngr's 5s capture targets a ~5s average update.
# Lower this (and/or the mngr streaming_snapshot_interval_seconds) only if the UI
# feels laggy.
STREAM_POLL_INTERVAL_SECONDS = 5.0


class AgentStreamWatcher:
    """Polls an agent's ``stream_buffer`` and broadcasts in-progress responses.

    The buffer file is allowed to not exist (streaming disabled, or not written
    yet); the watcher simply finds nothing and stays silent until it appears.
    """

    def __init__(
        self,
        agent_id: str,
        agent_state_dir: Path,
        on_events: Callable[[str, list[dict[str, Any]]], None],
        poll_interval_seconds: float = STREAM_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._agent_id = agent_id
        self._buffer_path = agent_state_dir / "plugin" / "claude" / "stream_buffer"
        self._on_events = on_events
        self._poll_interval_seconds = poll_interval_seconds

        # The last (last_complete_id, body) pair we broadcast. None until the
        # first broadcast, so a buffer that is empty/absent at startup does not
        # emit a redundant "clear" frame before anything has streamed.
        self._last_payload: tuple[str, str] | None = None

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"stream-watcher-{self._agent_id}")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            # wait() returns True when stopped, so a stop during the interval
            # breaks promptly instead of sleeping out the full window.
            if self._stop_event.wait(timeout=self._poll_interval_seconds):
                break
            self._poll_once()

    def _poll_once(self) -> None:
        payload = self._read_buffer()
        if payload is None:
            # Buffer missing/unreadable: streaming disabled or the file vanished
            # (e.g. agent destroyed). Leave the last broadcast state untouched.
            return
        previous = self._last_payload
        if payload == previous:
            return
        self._last_payload = payload
        last_complete_id, body = payload
        previous_body = previous[1] if previous is not None else ""
        # Suppress empty-body frames unless they clear a bubble we actually put
        # up: an empty body with no prior streamed text is just idle id-line
        # churn (the agent isn't typing, or a complete message landed while
        # idle), with nothing to show and nothing to clear.
        if body == "" and previous_body == "":
            return
        self._on_events(self._agent_id, [self._streaming_message(last_complete_id, body)])

    def _read_buffer(self) -> tuple[str, str] | None:
        """Return ``(last_complete_id, body)`` from the buffer, or None if absent.

        Line 1 is the uuid of the last complete assistant message; the remaining
        lines are the in-progress markdown body. A buffer with only the id line
        (the idle state) yields an empty body.
        """
        try:
            content = self._buffer_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        # The buffer always has at least the id line; split off that first line
        # and keep the rest verbatim as the body (it may itself be empty).
        first_newline = content.find("\n")
        if first_newline == -1:
            return content.strip(), ""
        last_complete_id = content[:first_newline].strip()
        body = content[first_newline + 1 :]
        return last_complete_id, body

    def _streaming_message(self, last_complete_id: str, body: str) -> dict[str, Any]:
        """The SSE message carrying the current in-progress assistant response.

        No ``session_id`` field: the main stream forwards events without one
        (``is_main_session_event``), while per-subagent streams keep only their
        own session -- so this preview shows on the main thread and never leaks
        into a subagent view.
        """
        return {"type": STREAMING_MESSAGE_TYPE, "last_complete_id": last_complete_id, "text": body}
