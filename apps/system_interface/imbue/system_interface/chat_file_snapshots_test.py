from pathlib import Path

from imbue.system_interface.chat_file_snapshots import ChatFileSnapshotStore
from imbue.system_interface.chat_file_snapshots import extract_referenced_paths


def test_extract_referenced_paths_returns_image_and_link_paths() -> None:
    text = (
        "Here is a chart:\n"
        "![Revenue](/mngr/code/runtime/chat-images/revenue.png)\n"
        "and a report [link](/mngr/code/runtime/chat-files/report.pdf)\n"
        "plus an external image ![ext](https://example.com/pic.png)\n"
        "and a relative one ![rel](runtime/chat-images/x.png)"
    )
    assert extract_referenced_paths(text) == [
        "/mngr/code/runtime/chat-images/revenue.png",
        "/mngr/code/runtime/chat-files/report.pdf",
    ]


def test_extract_referenced_paths_includes_non_image_files() -> None:
    assert extract_referenced_paths("![f](/tmp/data.csv)") == ["/tmp/data.csv"]


def test_extract_referenced_paths_excludes_app_routes() -> None:
    text = "[a](/api/uploads/x) and [b](/assets/app.js) and [c](/service/foo) and [d](//evil.com/x)"
    assert extract_referenced_paths(text) == []


def test_snapshot_freezes_bytes_at_first_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "chart.png"
    source.write_bytes(b"original")
    store = ChatFileSnapshotStore(tmp_path / "snapshots")

    first = store.snapshot("event-1", str(source))
    assert first is not None
    assert first.read_bytes() == b"original"

    source.write_bytes(b"changed")
    again = store.snapshot("event-1", str(source))
    assert again == first
    assert again is not None
    assert again.read_bytes() == b"original"


def test_snapshot_freezes_non_image_files(tmp_path: Path) -> None:
    source = tmp_path / "report.pdf"
    source.write_bytes(b"pdf-original")
    store = ChatFileSnapshotStore(tmp_path / "snapshots")

    first = store.snapshot("event-1", str(source))
    assert first is not None
    assert first.read_bytes() == b"pdf-original"
    assert first.suffix == ".pdf"

    source.write_bytes(b"pdf-changed")
    again = store.snapshot("event-1", str(source))
    assert again is not None
    assert again.read_bytes() == b"pdf-original"


def test_snapshot_takes_fresh_copy_for_new_event(tmp_path: Path) -> None:
    source = tmp_path / "chart.png"
    source.write_bytes(b"original")
    store = ChatFileSnapshotStore(tmp_path / "snapshots")
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
    store = ChatFileSnapshotStore(tmp_path / "snapshots")
    first = store.snapshot("event-1", str(source))
    second = store.snapshot("event-2", str(source))
    assert first == second


def test_snapshot_returns_none_for_missing_source(tmp_path: Path) -> None:
    store = ChatFileSnapshotStore(tmp_path / "snapshots")
    assert store.snapshot("event-1", str(tmp_path / "missing.png")) is None


def test_index_persists_across_store_instances(tmp_path: Path) -> None:
    source = tmp_path / "chart.png"
    source.write_bytes(b"original")
    snapshots_dir = tmp_path / "snapshots"
    first = ChatFileSnapshotStore(snapshots_dir).snapshot("event-1", str(source))
    assert first is not None

    source.write_bytes(b"changed")
    reloaded = ChatFileSnapshotStore(snapshots_dir).snapshot("event-1", str(source))
    assert reloaded == first
    assert reloaded is not None
    assert reloaded.read_bytes() == b"original"


def test_enqueue_events_snapshots_referenced_files(tmp_path: Path) -> None:
    image = tmp_path / "chart.png"
    image.write_bytes(b"img-original")
    report = tmp_path / "report.pdf"
    report.write_bytes(b"pdf-original")
    store = ChatFileSnapshotStore(tmp_path / "snapshots")
    event = {
        "event_id": "event-1",
        "type": "assistant_message",
        "text": f"Look: ![chart]({image}) and [report]({report})",
    }
    store.enqueue_events([event])
    # stop() drains by joining the worker thread after the sentinel, so the
    # queued snapshots are guaranteed to have been attempted once it returns.
    store.stop()

    image.write_bytes(b"img-changed")
    report.write_bytes(b"pdf-changed")
    frozen_image = store.snapshot("event-1", str(image))
    frozen_report = store.snapshot("event-1", str(report))
    assert frozen_image is not None
    assert frozen_image.read_bytes() == b"img-original"
    assert frozen_report is not None
    assert frozen_report.read_bytes() == b"pdf-original"


def test_enqueue_events_ignores_events_without_files(tmp_path: Path) -> None:
    store = ChatFileSnapshotStore(tmp_path / "snapshots")
    store.enqueue_events([{"event_id": "event-1", "type": "assistant_message", "text": "no files here"}])
    store.stop()
    assert not (tmp_path / "snapshots").exists()
