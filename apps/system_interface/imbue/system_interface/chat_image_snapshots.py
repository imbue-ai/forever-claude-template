"""Freeze chat-referenced images so every message keeps the bytes it was posted with.

Chat markdown references an image by its absolute on-disk path (see
``file_serving.py``), and that path is served with a one-year ``immutable``
cache policy. That policy is only correct if the bytes behind a URL never
change -- but nothing stops an agent from overwriting a previously referenced
file. Two failure modes result: a *new* message referencing an overwritten path
renders the browser's stale cached copy, and an *old* message re-rendered from
a cold cache shows the file's new content instead of what was originally
posted.

This module fixes both by snapshotting each referenced image per message. The
frontend rewrites an image's URL to ``/api/chat-images/<event_id>/<path>``; the
store maps ``(event_id, source_path)`` to a content-hashed copy taken the first
time that pair is seen, and the endpoint serves the copy. A new message gets a
new event id -- a URL the browser has never cached -- and an old message's URL
resolves to frozen bytes forever, making the ``immutable`` policy genuinely
true.

Snapshots are taken eagerly when the session watcher first emits an event (as
close to post time as the server can get) and lazily on first fetch as a
fallback for events that predate the feature or streamed while the server was
down. Copies are stored under ``blobs/<sha256><ext>`` so identical content is
deduplicated across messages, with the ``(event_id, source_path) -> blob``
mapping appended to ``index.jsonl``.
"""

import hashlib
import json
import os
import queue
import re
import threading
from pathlib import Path
from typing import Any

from loguru import logger

from imbue.system_interface.file_serving import image_mime_type_for_path

# Matches the target of markdown image syntax when it is an absolute on-disk
# path: ``![alt](/some/path.png)``. Whether the path is actually an inline
# image (by extension) is decided by ``image_mime_type_for_path``, not here.
_MARKDOWN_IMAGE_PATH_PATTERN = re.compile(r"!\[[^\]]*\]\(\s*(/[^)\s]+)\s*\)")


def extract_image_paths(markdown_text: str) -> list[str]:
    """Return the absolute inline-image paths referenced by ``markdown_text``."""
    paths: list[str] = []
    for match in _MARKDOWN_IMAGE_PATH_PATTERN.finditer(markdown_text):
        path = match.group(1)
        if image_mime_type_for_path(path) is not None:
            paths.append(path)
    return paths


class ChatImageSnapshotStore:
    """Per-message immutable copies of chat-referenced images.

    Thread-safe: the request threads (lazy snapshot on fetch) and the worker
    thread (eager snapshot on new events) both go through :meth:`snapshot`
    under one lock. The worker thread starts lazily on the first enqueued
    event, so tests and callers that never see events pay nothing.
    """

    def __init__(self, snapshots_dir: Path) -> None:
        self._blobs_dir = snapshots_dir / "blobs"
        self._index_path = snapshots_dir / "index.jsonl"
        self._lock = threading.Lock()
        # (event_id, source_path) -> blob file name; None until first use so
        # construction never touches the filesystem.
        self._blob_name_by_key: dict[tuple[str, str], str] | None = None
        self._work_queue: queue.SimpleQueue[tuple[str, str] | None] = queue.SimpleQueue()
        self._worker: threading.Thread | None = None

    def enqueue_events(self, events: list[dict[str, Any]]) -> None:
        """Queue eager snapshots for every image referenced by ``events``.

        Accepts the session watcher's parsed event dicts; assistant messages
        carry their markdown in ``text`` and user messages in ``content``.
        Returns immediately -- the file I/O happens on the worker thread, so
        this is safe to call from the watcher's event fan-out.
        """
        pending: list[tuple[str, str]] = []
        for event in events:
            event_id = event.get("event_id")
            if not isinstance(event_id, str) or event_id == "":
                continue
            for field in ("text", "content"):
                value = event.get(field)
                if isinstance(value, str) and value:
                    for path in extract_image_paths(value):
                        pending.append((event_id, path))
        if not pending:
            return
        with self._lock:
            if self._worker is None:
                self._worker = threading.Thread(target=self._run_worker, daemon=True, name="chat-image-snapshots")
                self._worker.start()
        for item in pending:
            self._work_queue.put(item)

    def stop(self) -> None:
        """Stop the worker thread (if it ever started). Idempotent."""
        with self._lock:
            worker = self._worker
            self._worker = None
        if worker is None:
            return
        self._work_queue.put(None)
        worker.join(timeout=5.0)

    def snapshot(self, event_id: str, source_path: str) -> Path | None:
        """Return the frozen copy for ``(event_id, source_path)``, creating it if needed.

        On the first call for a pair, the source file's current bytes are
        copied to a content-hashed blob and the mapping is recorded; later
        calls return that same blob regardless of what has happened to the
        source file since. Returns None when no snapshot exists and the source
        file is missing (or is not an inline-image path).
        """
        if image_mime_type_for_path(source_path) is None:
            return None
        with self._lock:
            index = self._ensure_index_loaded_locked()
            key = (event_id, source_path)
            existing_blob_name = index.get(key)
            if existing_blob_name is not None:
                blob_path = self._blobs_dir / existing_blob_name
                return blob_path if blob_path.is_file() else None

            source = Path(source_path)
            if not source.is_file():
                return None
            content = source.read_bytes()
            blob_name = hashlib.sha256(content).hexdigest() + source.suffix.lower()
            blob_path = self._blobs_dir / blob_name
            if not blob_path.is_file():
                self._blobs_dir.mkdir(parents=True, exist_ok=True)
                # Write-then-rename so a crash mid-write never leaves a
                # truncated blob behind a hash that claims full content.
                temporary_path = blob_path.with_name(blob_name + ".tmp")
                temporary_path.write_bytes(content)
                os.replace(temporary_path, blob_path)
            with self._index_path.open("a", encoding="utf-8") as index_file:
                index_file.write(
                    json.dumps({"event_id": event_id, "source_path": source_path, "blob_name": blob_name}) + "\n"
                )
            index[key] = blob_name
            return blob_path

    def _ensure_index_loaded_locked(self) -> dict[tuple[str, str], str]:
        if self._blob_name_by_key is not None:
            return self._blob_name_by_key
        index: dict[tuple[str, str], str] = {}
        if self._index_path.is_file():
            for line in self._index_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    # A torn final line from a crash mid-append; the pair it
                    # described will simply be re-snapshotted on next use.
                    logger.warning("Skipping unparseable chat image snapshot index line: {}", line)
                    continue
                index[(entry["event_id"], entry["source_path"])] = entry["blob_name"]
        self._blob_name_by_key = index
        return index

    def _run_worker(self) -> None:
        # Blocks on the queue until the None sentinel from stop() ends the loop.
        for item in iter(self._work_queue.get, None):
            event_id, source_path = item
            try:
                self.snapshot(event_id, source_path)
            except OSError as e:
                # Eager snapshotting is best-effort; the serve endpoint retries
                # lazily on first fetch.
                logger.warning("Eager chat image snapshot failed for {}: {}", source_path, e)
