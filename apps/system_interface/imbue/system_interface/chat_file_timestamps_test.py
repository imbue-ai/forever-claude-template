import os
from pathlib import Path

from imbue.system_interface.chat_file_timestamps import ChatFileStatus
from imbue.system_interface.chat_file_timestamps import ChatFileTimestampStore
from imbue.system_interface.chat_file_timestamps import extract_referenced_paths


def _rewind_mtime(path: Path) -> None:
    """Backdate the file's mtime so a same-instant rewrite still reads as changed."""
    stat_result = path.stat()
    os.utime(path, ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns - 1_000_000_000))


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


def test_check_records_on_first_sight_and_stays_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "chart.png"
    source.write_bytes(b"original")
    store = ChatFileTimestampStore(tmp_path / "store")
    assert store.check("event-1", str(source)) is ChatFileStatus.UNCHANGED
    assert store.check("event-1", str(source)) is ChatFileStatus.UNCHANGED


def test_check_reports_changed_after_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "chart.png"
    source.write_bytes(b"original")
    store = ChatFileTimestampStore(tmp_path / "store")
    assert store.check("event-1", str(source)) is ChatFileStatus.UNCHANGED

    _rewind_mtime(source)
    source.write_bytes(b"changed!")
    assert store.check("event-1", str(source)) is ChatFileStatus.CHANGED


def test_check_reports_changed_for_non_image_file(tmp_path: Path) -> None:
    source = tmp_path / "report.pdf"
    source.write_bytes(b"pdf-original")
    store = ChatFileTimestampStore(tmp_path / "store")
    assert store.check("event-1", str(source)) is ChatFileStatus.UNCHANGED

    _rewind_mtime(source)
    source.write_bytes(b"pdf-changed")
    assert store.check("event-1", str(source)) is ChatFileStatus.CHANGED


def test_check_reports_changed_after_delete(tmp_path: Path) -> None:
    source = tmp_path / "chart.png"
    source.write_bytes(b"original")
    store = ChatFileTimestampStore(tmp_path / "store")
    assert store.check("event-1", str(source)) is ChatFileStatus.UNCHANGED

    source.unlink()
    assert store.check("event-1", str(source)) is ChatFileStatus.CHANGED


def test_new_event_records_current_content_after_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "chart.png"
    source.write_bytes(b"original")
    store = ChatFileTimestampStore(tmp_path / "store")
    assert store.check("event-1", str(source)) is ChatFileStatus.UNCHANGED

    _rewind_mtime(source)
    source.write_bytes(b"changed!")
    # A new message referencing the overwritten path records the file as it is
    # now, so the new message renders fine while the old one reports CHANGED.
    assert store.check("event-2", str(source)) is ChatFileStatus.UNCHANGED
    assert store.check("event-1", str(source)) is ChatFileStatus.CHANGED


def test_check_reports_unknown_for_never_seen_missing_file(tmp_path: Path) -> None:
    store = ChatFileTimestampStore(tmp_path / "store")
    assert store.check("event-1", str(tmp_path / "missing.png")) is ChatFileStatus.UNKNOWN


def test_index_persists_across_store_instances(tmp_path: Path) -> None:
    source = tmp_path / "chart.png"
    source.write_bytes(b"original")
    store_dir = tmp_path / "store"
    assert ChatFileTimestampStore(store_dir).check("event-1", str(source)) is ChatFileStatus.UNCHANGED

    _rewind_mtime(source)
    source.write_bytes(b"changed!")
    assert ChatFileTimestampStore(store_dir).check("event-1", str(source)) is ChatFileStatus.CHANGED


def test_enqueue_events_records_referenced_files(tmp_path: Path) -> None:
    image = tmp_path / "chart.png"
    image.write_bytes(b"img-original")
    report = tmp_path / "report.pdf"
    report.write_bytes(b"pdf-original")
    store = ChatFileTimestampStore(tmp_path / "store")
    event = {
        "event_id": "event-1",
        "type": "assistant_message",
        "text": f"Look: ![chart]({image}) and [report]({report})",
    }
    store.enqueue_events([event])
    # stop() drains by joining the worker thread after the sentinel, so the
    # queued records are guaranteed to have been attempted once it returns.
    store.stop()

    _rewind_mtime(image)
    image.write_bytes(b"img-changed")
    _rewind_mtime(report)
    report.write_bytes(b"pdf-changed")
    assert store.check("event-1", str(image)) is ChatFileStatus.CHANGED
    assert store.check("event-1", str(report)) is ChatFileStatus.CHANGED


def test_enqueue_events_ignores_events_without_files(tmp_path: Path) -> None:
    store = ChatFileTimestampStore(tmp_path / "store")
    store.enqueue_events([{"event_id": "event-1", "type": "assistant_message", "text": "no files here"}])
    store.stop()
    assert not (tmp_path / "store").exists()
