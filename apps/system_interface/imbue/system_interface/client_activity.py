"""Append-only client-activity event log for the workspace.

Records which browser client did what, so agents can work out which client
(and therefore which named layout) a request came from. Three event types
land in ``<workspace_layout_dir>/events/client_activity/events.jsonl``:

- ``message``: a chat message sent through the UI (text truncated at write
  time; full transcripts live in the agents' own session files).
- ``layout_switch``: a client changed its active layout (any initiator --
  user action, agent-driven load, or delete fallback).
- ``client_connected``: a client registered itself over the WebSocket.

Every line carries the standard event envelope plus the client id, the
device kind (mobile/desktop, derived from the user agent client-side), and
the layout involved. The file is append-only and unrotated.
"""

import json
import threading
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger as _loguru_logger
from pydantic import Field

from imbue.imbue_common.event_envelope import EventEnvelope
from imbue.imbue_common.event_envelope import EventId
from imbue.imbue_common.event_envelope import EventSource
from imbue.imbue_common.event_envelope import EventType
from imbue.imbue_common.event_envelope import IsoTimestamp
from imbue.imbue_common.logging import format_nanosecond_iso_timestamp
from imbue.imbue_common.logging import generate_log_event_id
from imbue.imbue_common.pure import pure

CLIENT_ACTIVITY_EVENT_SOURCE: Final[EventSource] = EventSource("client_activity")

MESSAGE_EVENT_TYPE: Final[EventType] = EventType("message")
LAYOUT_SWITCH_EVENT_TYPE: Final[EventType] = EventType("layout_switch")
CLIENT_CONNECTED_EVENT_TYPE: Final[EventType] = EventType("client_connected")

# Message text is truncated at write time: the log exists to disambiguate
# "which client asked me this", not to duplicate the agents' transcripts.
MESSAGE_TEXT_TRUNCATION_LIMIT: Final[int] = 500

# How many recent messages each client contributes to the ``context`` summary.
RECENT_MESSAGES_PER_CLIENT: Final[int] = 5

# Serializes appends across the threaded WSGI server's request threads.
_append_lock = threading.Lock()


class ClientMessageEvent(EventEnvelope):
    """A chat message sent through the UI, with the sending client's identity."""

    client_id: str = Field(description="Per-browser client id (uuid minted client-side)")
    device_kind: str = Field(description="'mobile' or 'desktop', derived from the user agent")
    layout_slug: str = Field(description="The client's active layout at send time")
    agent_id: str = Field(description="Id of the agent the message was sent to")
    agent_name: str = Field(description="Name of the agent the message was sent to")
    message_text: str = Field(description="Message text, truncated at write time")
    is_message_truncated: bool = Field(description="Whether message_text was truncated")


class LayoutSwitchEvent(EventEnvelope):
    """A client changed its active layout."""

    client_id: str = Field(description="Per-browser client id")
    device_kind: str = Field(description="'mobile' or 'desktop'")
    from_layout_slug: str = Field(description="The previously-active layout slug ('' when unknown)")
    to_layout_slug: str = Field(description="The newly-active layout slug")


class ClientConnectedEvent(EventEnvelope):
    """A client registered itself (with its active layout) over the WebSocket."""

    client_id: str = Field(description="Per-browser client id")
    device_kind: str = Field(description="'mobile' or 'desktop'")
    layout_slug: str = Field(description="The client's active layout at connect time")


def get_events_path(layout_dir: Path) -> Path:
    """Where the client-activity event log lives under one workspace_layout dir."""
    return layout_dir / "events" / "client_activity" / "events.jsonl"


def _now_iso() -> IsoTimestamp:
    return IsoTimestamp(format_nanosecond_iso_timestamp(datetime.now(timezone.utc)))


def _new_event_id() -> EventId:
    return EventId(generate_log_event_id())


@pure
def truncate_message_text(text: str) -> tuple[str, bool]:
    if len(text) <= MESSAGE_TEXT_TRUNCATION_LIMIT:
        return text, False
    return text[:MESSAGE_TEXT_TRUNCATION_LIMIT], True


def _append_event(events_path: Path, event: EventEnvelope) -> None:
    with _append_lock:
        events_path.parent.mkdir(parents=True, exist_ok=True)
        with events_path.open("a") as event_file:
            event_file.write(event.model_dump_json() + "\n")


def append_message_event(
    events_path: Path,
    client_id: str,
    device_kind: str,
    layout_slug: str,
    agent_id: str,
    agent_name: str,
    message_text: str,
) -> None:
    truncated_text, is_truncated = truncate_message_text(message_text)
    _append_event(
        events_path,
        ClientMessageEvent(
            timestamp=_now_iso(),
            type=MESSAGE_EVENT_TYPE,
            event_id=_new_event_id(),
            source=CLIENT_ACTIVITY_EVENT_SOURCE,
            client_id=client_id,
            device_kind=device_kind,
            layout_slug=layout_slug,
            agent_id=agent_id,
            agent_name=agent_name,
            message_text=truncated_text,
            is_message_truncated=is_truncated,
        ),
    )


def append_layout_switch_event(
    events_path: Path,
    client_id: str,
    device_kind: str,
    from_layout_slug: str,
    to_layout_slug: str,
) -> None:
    _append_event(
        events_path,
        LayoutSwitchEvent(
            timestamp=_now_iso(),
            type=LAYOUT_SWITCH_EVENT_TYPE,
            event_id=_new_event_id(),
            source=CLIENT_ACTIVITY_EVENT_SOURCE,
            client_id=client_id,
            device_kind=device_kind,
            from_layout_slug=from_layout_slug,
            to_layout_slug=to_layout_slug,
        ),
    )


def append_client_connected_event(
    events_path: Path,
    client_id: str,
    device_kind: str,
    layout_slug: str,
) -> None:
    _append_event(
        events_path,
        ClientConnectedEvent(
            timestamp=_now_iso(),
            type=CLIENT_CONNECTED_EVENT_TYPE,
            event_id=_new_event_id(),
            source=CLIENT_ACTIVITY_EVENT_SOURCE,
            client_id=client_id,
            device_kind=device_kind,
            layout_slug=layout_slug,
        ),
    )


def read_client_activity_events(events_path: Path) -> list[dict[str, Any]]:
    """Every parseable event line, in file (chronological) order."""
    if not events_path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in events_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as e:
            _loguru_logger.opt(exception=e).warning("Skipped unparsable client-activity event line")
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


@pure
def _event_layout_slug(event: dict[str, Any]) -> str | None:
    """The layout the event says its client is on afterwards, or None."""
    event_type = event.get("type")
    if event_type == LAYOUT_SWITCH_EVENT_TYPE:
        return str(event.get("to_layout_slug", "")) or None
    if event_type in (MESSAGE_EVENT_TYPE, CLIENT_CONNECTED_EVENT_TYPE):
        return str(event.get("layout_slug", "")) or None
    return None


@pure
def summarize_client_activity(
    events: Sequence[dict[str, Any]],
    connected_client_ids: AbstractSet[str],
) -> list[dict[str, Any]]:
    """Fold the event log into one summary per client, most recently seen first.

    Each entry carries the client's id, latest device kind, current layout
    (from its most recent event), last-seen timestamp, whether it is
    currently connected, and its last few messages (oldest first).
    """
    summary_by_client_id: dict[str, dict[str, Any]] = {}
    for event in events:
        client_id = str(event.get("client_id", ""))
        if not client_id:
            continue
        summary = summary_by_client_id.setdefault(
            client_id,
            {
                "client_id": client_id,
                "device_kind": "",
                "current_layout": None,
                "last_seen": "",
                "is_connected": False,
                "recent_messages": [],
            },
        )
        summary["last_seen"] = str(event.get("timestamp", ""))
        device_kind = str(event.get("device_kind", ""))
        if device_kind:
            summary["device_kind"] = device_kind
        layout_slug = _event_layout_slug(event)
        if layout_slug is not None:
            summary["current_layout"] = layout_slug
        if event.get("type") == MESSAGE_EVENT_TYPE:
            summary["recent_messages"].append(
                {
                    "timestamp": str(event.get("timestamp", "")),
                    "agent_name": str(event.get("agent_name", "")),
                    "text": str(event.get("message_text", "")),
                }
            )
            del summary["recent_messages"][:-RECENT_MESSAGES_PER_CLIENT]
    for client_id in connected_client_ids:
        if client_id in summary_by_client_id:
            summary_by_client_id[client_id]["is_connected"] = True
    return sorted(summary_by_client_id.values(), key=lambda s: s["last_seen"], reverse=True)


@pure
def find_client_id_for_agent(events: Sequence[dict[str, Any]], agent_id: str) -> str | None:
    """The client that most recently messaged ``agent_id``, or None.

    This is how an agent-initiated op is attributed back to "the client that
    asked for it": the requester's most recent message event names them.
    """
    if not agent_id:
        return None
    for event in reversed(events):
        if event.get("type") == MESSAGE_EVENT_TYPE and event.get("agent_id") == agent_id:
            client_id = str(event.get("client_id", ""))
            return client_id or None
    return None
