"""The browser-fleet persistence manifest: the one thing we serialize by hand.

It records the durable *topology* of the fleet -- which browser names exist and the
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
# v2: browser ids are now random NAME strings (not sequential ints), and the
# ``next_id`` high-water mark is gone. ``read_manifest`` rejects any other version
# (see below), so an older v1 (int-id) manifest is treated as missing rather than
# silently coerced -- the fleet then re-scans the on-disk profiles instead.
_MANIFEST_VERSION = 2


class ManifestEntry(MutableModel):
    """One persisted browser: its name and the tab URLs to reopen (active tab marked)."""

    id: str
    tabs: list[str] = []  # ordered tab URLs; blank/about:/chrome: are dropped before saving
    active_tab: int = 0


class Manifest(MutableModel):
    """The whole fleet's durable topology (names + tabs). No id high-water mark: ids
    are random names, generated on demand and de-duplicated against the live fleet."""

    version: int = _MANIFEST_VERSION
    browsers: list[ManifestEntry] = []


def manifest_path() -> Path:
    return _MANIFEST_PATH


def read_manifest() -> Manifest | None:
    """Load the manifest, or ``None`` if it's absent, unreadable/corrupt, or an OLD version.

    A corrupt manifest is treated as "missing" (and logged loudly) rather than
    crashing startup -- the caller then cross-checks the on-disk profiles to decide
    first-boot vs manifest-loss (see ``session.py`` restore).

    The version is checked EXPLICITLY: a non-current version (e.g. a pre-name v1 file
    whose int ids pydantic would otherwise happily coerce to strings) is treated as
    missing, so an upgrade across the int->name change starts from an empty manifest
    rather than resurrecting numeric ids. The profiles are still re-scanned, and
    pure-numeric profile-dir suffixes are skipped (see session._scan_profile_names),
    so old numeric browsers are not silently revived under string names."""
    path = _MANIFEST_PATH
    if not path.exists():
        return None
    try:
        loaded = Manifest.model_validate_json(path.read_text())
    except (OSError, ValueError, ValidationError) as e:
        logger.warning("browser-fleet manifest at {} is unreadable ({}); ignoring it", path, e)
        return None
    if loaded.version != _MANIFEST_VERSION:
        logger.warning(
            "browser-fleet manifest at {} is version {} (expected {}); ignoring it and re-scanning profiles",
            path,
            loaded.version,
            _MANIFEST_VERSION,
        )
        return None
    return loaded


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
