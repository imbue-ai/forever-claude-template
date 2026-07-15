import json
from pathlib import Path

from imbue.system_interface.client_activity import MESSAGE_TEXT_TRUNCATION_LIMIT
from imbue.system_interface.client_activity import append_client_connected_event
from imbue.system_interface.client_activity import append_layout_switch_event
from imbue.system_interface.client_activity import append_message_event
from imbue.system_interface.client_activity import find_client_id_for_agent
from imbue.system_interface.client_activity import read_client_activity_events
from imbue.system_interface.client_activity import summarize_client_activity
from imbue.system_interface.client_activity import truncate_message_text


def _events_path(tmp_path: Path) -> Path:
    return tmp_path / "events" / "client_activity" / "events.jsonl"


def test_truncate_message_text() -> None:
    short_text, is_truncated = truncate_message_text("hello")
    assert (short_text, is_truncated) == ("hello", False)

    long_text, is_long_truncated = truncate_message_text("x" * (MESSAGE_TEXT_TRUNCATION_LIMIT + 1))
    assert len(long_text) == MESSAGE_TEXT_TRUNCATION_LIMIT
    assert is_long_truncated is True


def test_append_events_carry_standard_envelope(tmp_path: Path) -> None:
    events_path = _events_path(tmp_path)
    append_client_connected_event(events_path, client_id="c1", device_kind="desktop", layout_slug="desktop")
    append_layout_switch_event(
        events_path, client_id="c1", device_kind="desktop", from_layout_slug="desktop", to_layout_slug="mobile"
    )
    append_message_event(
        events_path,
        client_id="c1",
        device_kind="desktop",
        layout_slug="mobile",
        agent_id="agent-1",
        agent_name="alice",
        message_text="hi",
    )

    lines = [json.loads(line) for line in events_path.read_text().splitlines()]
    assert [event["type"] for event in lines] == ["client_connected", "layout_switch", "message"]
    for event in lines:
        assert event["source"] == "client_activity"
        assert event["event_id"].startswith("evt-")
        assert event["timestamp"].endswith("Z")
        # Nanosecond precision: 9 fractional digits before the Z.
        fractional = event["timestamp"].rsplit(".", 1)[1].rstrip("Z")
        assert len(fractional) == 9


def test_read_skips_unparsable_lines(tmp_path: Path) -> None:
    events_path = _events_path(tmp_path)
    append_client_connected_event(events_path, client_id="c1", device_kind="desktop", layout_slug="desktop")
    with events_path.open("a") as event_file:
        event_file.write("corrupt line{\n")
    append_client_connected_event(events_path, client_id="c2", device_kind="mobile", layout_slug="mobile")

    events = read_client_activity_events(events_path)
    assert [event["client_id"] for event in events] == ["c1", "c2"]


def test_read_missing_file_is_empty(tmp_path: Path) -> None:
    assert read_client_activity_events(_events_path(tmp_path)) == []


def test_summarize_client_activity_tracks_layout_and_messages(tmp_path: Path) -> None:
    events_path = _events_path(tmp_path)
    append_client_connected_event(events_path, client_id="c1", device_kind="desktop", layout_slug="desktop")
    for message_index in range(7):
        append_message_event(
            events_path,
            client_id="c1",
            device_kind="desktop",
            layout_slug="desktop",
            agent_id="agent-1",
            agent_name="alice",
            message_text=f"message {message_index}",
        )
    append_layout_switch_event(
        events_path, client_id="c1", device_kind="desktop", from_layout_slug="desktop", to_layout_slug="mobile"
    )
    append_message_event(
        events_path,
        client_id="c2",
        device_kind="mobile",
        layout_slug="mobile",
        agent_id="agent-2",
        agent_name="bob",
        message_text="from the phone",
    )

    events = read_client_activity_events(events_path)
    summaries = summarize_client_activity(events, connected_client_ids={"c2"})

    summary_by_id = {summary["client_id"]: summary for summary in summaries}
    assert set(summary_by_id) == {"c1", "c2"}
    # The switch event moved c1's current layout to mobile; only the last 5
    # of its 7 messages are kept, oldest first.
    c1 = summary_by_id["c1"]
    assert c1["current_layout"] == "mobile"
    assert c1["is_connected"] is False
    assert [message["text"] for message in c1["recent_messages"]] == [f"message {i}" for i in range(2, 7)]
    c2 = summary_by_id["c2"]
    assert c2["is_connected"] is True
    assert c2["device_kind"] == "mobile"
    assert c2["current_layout"] == "mobile"
    # Most recently seen client first.
    assert summaries[0]["client_id"] == "c2"


def test_find_client_id_for_agent_picks_most_recent(tmp_path: Path) -> None:
    events_path = _events_path(tmp_path)
    append_message_event(
        events_path,
        client_id="c1",
        device_kind="desktop",
        layout_slug="desktop",
        agent_id="agent-1",
        agent_name="alice",
        message_text="first",
    )
    append_message_event(
        events_path,
        client_id="c2",
        device_kind="mobile",
        layout_slug="mobile",
        agent_id="agent-1",
        agent_name="alice",
        message_text="second",
    )
    events = read_client_activity_events(events_path)

    assert find_client_id_for_agent(events, "agent-1") == "c2"
    assert find_client_id_for_agent(events, "agent-unknown") is None
    assert find_client_id_for_agent(events, "") is None
