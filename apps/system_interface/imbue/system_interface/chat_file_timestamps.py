"""Detect when a chat-referenced file changed after its message was posted.

Chat markdown references a file by its absolute on-disk path (see
``file_serving.py``): ``![chart](/.../chart.png)`` renders an image inline and
``[report](/.../report.pdf)`` becomes a download link. Nothing stops an agent
from later overwriting a referenced file, which would silently change what an
already-posted message shows (or downloads) -- and, for images with long-lived
browser caching, could leave a *new* message showing stale cached bytes.

This module detects that without copying any files. The frontend rewrites the
URL -- an image ``src`` or a link ``href`` -- to ``/api/chat-files/<event_id>/
<path>``; the store records the source file's modification time and size the
first time each ``(event_id, source_path)`` pair is seen (eagerly when the
session watcher emits the event -- as close to post time as the server can get
-- and lazily on first fetch as a fallback). The endpoint serves the live file
with caching disabled, so every render refetches; if the file no longer matches
its recorded fingerprint, the endpoint reports it changed and the frontend
replaces the image or link with a plain notice that the file is stale.

Records are appended to ``index.jsonl`` under the store directory.
"""

import json
import queue
import re
import threading
from enum import Enum
from pathlib import Path
from typing import Any

from loguru import logger

# Matches the target of markdown image or link syntax when it is an absolute
# on-disk path: ``![alt](/some/path.png)`` or ``[label](/some/report.pdf)``.
# The optional leading ``!`` covers both; the target must start with ``/``.
_MARKDOWN_REFERENCE_PATH_PATTERN = re.compile(r"!?\[[^\]]*\]\(\s*(/[^)\s]+)\s*\)")

# App routes that share the absolute-path shape but are not on-disk files, so
# they must never be change-checked (mirrors the frontend's rewrite guard).
_RESERVED_URL_PREFIXES = ("/api/", "/assets/", "/plugins/", "/service/", "/_")


def _is_change_checkable_path(path: str) -> bool:
    if path.startswith("//"):
        return False
    return not path.startswith(_RESERVED_URL_PREFIXES)


def extract_referenced_paths(markdown_text: str) -> list[str]:
    """Return the absolute on-disk file paths referenced by ``markdown_text``.

    Covers both inline images (``![alt](/path)``) and download links
    (``[label](/path)``); app-route paths (``/api/...`` etc.) are excluded.
    """
    paths: list[str] = []
    for match in _MARKDOWN_REFERENCE_PATH_PATTERN.finditer(markdown_text):
        path = match.group(1)
        if _is_change_checkable_path(path):
            paths.append(path)
    return paths


class ChatFileStatus(Enum):
    """Whether a file still matches the fingerprint recorded for its message."""

    # The file exists and matches the recorded (or just-recorded) fingerprint.
    UNCHANGED = "unchanged"
    # The file's fingerprint no longer matches the one recorded for this
    # message (including the file having been deleted after being recorded).
    CHANGED = "changed"
    # No fingerprint is recorded and the file does not exist, so there is
    # nothing to compare -- e.g. a typo'd path (renders as a broken image /
    # dead link).
    UNKNOWN = "unknown"


class ChatFileTimestampStore:
    """Per-message fingerprints (mtime + size) of chat-referenced files.

    Thread-safe: the request threads (lazy record on fetch) and the worker
    thread (eager record on new events) both go through the one lock. The
    worker thread starts lazily on the first enqueued event, so tests and
    callers that never see events pay nothing.
    """

    def __init__(self, store_dir: Path) -> None:
        self._index_path = store_dir / "index.jsonl"
        self._lock = threading.Lock()
        # (event_id, source_path) -> (mtime_ns, size); None until first use so
        # construction never touches the filesystem.
        self._fingerprint_by_key: dict[tuple[str, str], tuple[int, int]] | None = None
        self._work_queue: queue.SimpleQueue[tuple[str, str] | None] = queue.SimpleQueue()
        self._worker: threading.Thread | None = None

    def enqueue_events(self, events: list[dict[str, Any]]) -> None:
        """Queue eager fingerprint records for every file referenced by ``events``.

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
                    for path in extract_referenced_paths(value):
                        pending.append((event_id, path))
        if not pending:
            return
        with self._lock:
            if self._worker is None:
                self._worker = threading.Thread(target=self._run_worker, daemon=True, name="chat-file-timestamps")
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

    def check(self, event_id: str, source_path: str) -> ChatFileStatus:
        """Compare the file behind ``source_path`` to the fingerprint for ``event_id``.

        The first call for an unseen pair records the file's current
        fingerprint (the lazy fallback for events that predate the feature or
        streamed while the server was down) and reports UNCHANGED; later calls
        report CHANGED once the file's mtime or size no longer matches, or
        once the file is gone.
        """
        with self._lock:
            index = self._ensure_index_loaded_locked()
            key = (event_id, source_path)
            current = self._fingerprint_of(source_path)
            recorded = index.get(key)
            if recorded is None:
                if current is None:
                    return ChatFileStatus.UNKNOWN
                self._append_record_locked(index, key, current)
                return ChatFileStatus.UNCHANGED
            if current == recorded:
                return ChatFileStatus.UNCHANGED
            return ChatFileStatus.CHANGED

    def record(self, event_id: str, source_path: str) -> None:
        """Record the file's current fingerprint for ``event_id`` if unseen.

        Used by the eager path; a pair that already has a fingerprint is left
        untouched, and a missing file records nothing (the lazy path in
        :meth:`check` will pick it up if the file appears before first fetch).
        """
        with self._lock:
            index = self._ensure_index_loaded_locked()
            key = (event_id, source_path)
            if key in index:
                return
            current = self._fingerprint_of(source_path)
            if current is None:
                return
            self._append_record_locked(index, key, current)

    def _fingerprint_of(self, source_path: str) -> tuple[int, int] | None:
        try:
            stat_result = Path(source_path).stat()
        except OSError:
            return None
        return (stat_result.st_mtime_ns, stat_result.st_size)

    def _append_record_locked(
        self, index: dict[tuple[str, str], tuple[int, int]], key: tuple[str, str], fingerprint: tuple[int, int]
    ) -> None:
        event_id, source_path = key
        mtime_ns, size = fingerprint
        self._index_path.parent.mkdir(parents=True, exist_ok=True)
        with self._index_path.open("a", encoding="utf-8") as index_file:
            index_file.write(
                json.dumps({"event_id": event_id, "source_path": source_path, "mtime_ns": mtime_ns, "size": size})
                + "\n"
            )
        index[key] = fingerprint

    def _ensure_index_loaded_locked(self) -> dict[tuple[str, str], tuple[int, int]]:
        if self._fingerprint_by_key is not None:
            return self._fingerprint_by_key
        index: dict[tuple[str, str], tuple[int, int]] = {}
        if self._index_path.is_file():
            for line in self._index_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    # A torn final line from a crash mid-append; the pair it
                    # described will simply be re-recorded on next use.
                    logger.warning("Skipping unparseable chat file timestamp index line: {}", line)
                    continue
                index[(entry["event_id"], entry["source_path"])] = (entry["mtime_ns"], entry["size"])
        self._fingerprint_by_key = index
        return index

    def _run_worker(self) -> None:
        # Blocks on the queue until the None sentinel from stop() ends the loop.
        for item in iter(self._work_queue.get, None):
            event_id, source_path = item
            try:
                self.record(event_id, source_path)
            except OSError as e:
                # Eager recording is best-effort; the serve endpoint records
                # lazily on first fetch.
                logger.warning("Eager chat file fingerprint record failed for {}: {}", source_path, e)
