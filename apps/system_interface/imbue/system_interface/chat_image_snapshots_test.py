from pathlib import Path

from imbue.system_interface.chat_image_snapshots import ChatImageSnapshotStore
from imbue.system_interface.chat_image_snapshots import extract_image_paths


def test_extract_image_paths_returns_absolute_inline_image_paths() -> None:
    text = (
        "Here is a chart:\n"
        "![Revenue](/mngr/code/runtime/chat-images/revenue.png)\n"
        "and a report [link](/mngr/code/runtime/chat-files/report.pdf)\n"
        "plus an external image ![ext](https://example.com/pic.png)\n"
        "and a relative one ![rel](runtime/chat-images/x.png)"
    )
    assert extract_image_paths(text) == ["/mngr/code/runtime/chat-images/revenue.png"]


def test_extract_image_paths_skips_non_image_extensions() -> None:
    assert extract_image_paths("![f](/tmp/data.csv)") == []


def test_snapshot_freezes_bytes_at_first_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "chart.png"
    source.write_bytes(b"original")
    store = ChatImageSnapshotStore(tmp_path / "snapshots")

    first = store.snapshot("event-1", str(source))
    assert first is not None
    assert first.read_bytes() == b"original"

    source.write_bytes(b"changed")
    again = store.snapshot("event-1", str(source))
    assert again == first
    assert again is not None
    assert again.read_bytes() == b"original"


def test_snapshot_takes_fresh_copy_for_new_event(tmp_path: Path) -> None:
    source = tmp_path / "chart.png"
    source.write_bytes(b"original")
    store = ChatImageSnapshotStore(tmp_path / "snapshots")
    first = store.snapshot("event-1", str(source))
    assert first is not None

    source.write_bytes(b"changed")
    second = store.snapshot("event-2", str(source))
    assert second is not None
    assert second.read_bytes() == b"changed"
    assert first.read_bytes() == b"original"


def test_snapshot_deduplicates_identical_content_across_events(tmp_path: Path) -> None:
    source = tmp_path / "chart.png"
    source.write_bytes(b"same bytes")
    store = ChatImageSnapshotStore(tmp_path / "snapshots")
    first = store.snapshot("event-1", str(source))
    second = store.snapshot("event-2", str(source))
    assert first == second


def test_snapshot_returns_none_for_missing_source(tmp_path: Path) -> None:
    store = ChatImageSnapshotStore(tmp_path / "snapshots")
    assert store.snapshot("event-1", str(tmp_path / "missing.png")) is None


def test_snapshot_returns_none_for_non_image_path(tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("hello")
    store = ChatImageSnapshotStore(tmp_path / "snapshots")
    assert store.snapshot("event-1", str(source)) is None


def test_index_persists_across_store_instances(tmp_path: Path) -> None:
    source = tmp_path / "chart.png"
    source.write_bytes(b"original")
    snapshots_dir = tmp_path / "snapshots"
    first = ChatImageSnapshotStore(snapshots_dir).snapshot("event-1", str(source))
    assert first is not None

    source.write_bytes(b"changed")
    reloaded = ChatImageSnapshotStore(snapshots_dir).snapshot("event-1", str(source))
    assert reloaded == first
    assert reloaded is not None
    assert reloaded.read_bytes() == b"original"


def test_enqueue_events_snapshots_referenced_images(tmp_path: Path) -> None:
    source = tmp_path / "chart.png"
    source.write_bytes(b"original")
    store = ChatImageSnapshotStore(tmp_path / "snapshots")
    event = {
        "event_id": "event-1",
        "type": "assistant_message",
        "text": f"Look: ![chart]({source})",
    }
    store.enqueue_events([event])
    # stop() drains by joining the worker thread after the sentinel, so the
    # queued snapshot is guaranteed to have been attempted once it returns.
    store.stop()

    source.write_bytes(b"changed")
    frozen = store.snapshot("event-1", str(source))
    assert frozen is not None
    assert frozen.read_bytes() == b"original"


def test_enqueue_events_ignores_events_without_images(tmp_path: Path) -> None:
    store = ChatImageSnapshotStore(tmp_path / "snapshots")
    store.enqueue_events([{"event_id": "event-1", "type": "assistant_message", "text": "no images here"}])
    store.stop()
    assert not (tmp_path / "snapshots").exists()
