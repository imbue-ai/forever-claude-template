"""The browser-fleet persistence manifest: the one thing we serialize by hand.

It records the durable *topology* of the fleet -- which browser ids exist and the
tab URLs each had -- so the daemon can relaunch them on the next container start.
It deliberately stores NO ownership/queue state (that is connection/process-scoped
and dies with the old container) and NO Chromium profile bytes (cookies/logins/
history live in each browser's persistent ``user_data_dir`` on the workspace volume;
see ``session.py``). Because it's tiny JSON it can live under ``runtime/`` and ride
the mindsbackup branch, so even a full container rebuild restores the tab list.

Pure synchronous file IO (no asyncio here, on purpose): writes are atomic via a
temp file + ``os.replace`` so a reader on the next boot sees either the old or the
new complete file, never a torn one.
"""

import json
import os
from pathlib import Path

from loguru import logger
from pydantic import ValidationError

from imbue.imbue_common.mutable_model import MutableModel

# Relative to the daemon's cwd (= repo root). Override for tests / alternate layouts.
_MANIFEST_PATH = Path(os.environ.get("BROWSER_MANIFEST_PATH", "runtime/browser-fleet.json"))
_MANIFEST_VERSION = 1


class ManifestEntry(MutableModel):
    """One persisted browser: its id and the tab URLs to reopen (active tab marked)."""

    id: int
    tabs: list[str] = []  # ordered tab URLs; blank/about:/chrome: are dropped before saving
    active_tab: int = 0


class Manifest(MutableModel):
    """The whole fleet's durable topology plus the monotonic id high-water mark."""

    version: int = _MANIFEST_VERSION
    next_id: int = 1
    browsers: list[ManifestEntry] = []


def manifest_path() -> Path:
    return _MANIFEST_PATH


def read_manifest() -> Manifest | None:
    """Load the manifest, or ``None`` if it's absent OR unreadable/corrupt.

    A corrupt manifest is treated as "missing" (and logged loudly) rather than
    crashing startup -- the caller then cross-checks the on-disk profiles to decide
    first-boot vs manifest-loss (see ``session.py`` restore)."""
    path = _MANIFEST_PATH
    if not path.exists():
        return None
    try:
        return Manifest.model_validate_json(path.read_text())
    except (OSError, ValueError, ValidationError) as e:
        logger.warning("browser-fleet manifest at {} is unreadable ({}); ignoring it", path, e)
        return None


def write_manifest(manifest: Manifest) -> None:
    """Atomically persist the manifest: write a sibling ``.tmp`` (flushed + fsynced),
    then ``os.replace`` it into place (POSIX-atomic on macOS and Linux)."""
    path = _MANIFEST_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = manifest.model_dump_json()
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
