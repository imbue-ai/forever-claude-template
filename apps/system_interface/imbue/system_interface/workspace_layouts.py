"""Named workspace-layout storage for the system interface.

The dockview layout used to be a single implicit ``layout.json``. It is now a
set of *named layouts*, each stored as its own JSON file under
``<workspace_layout_dir>/layouts/<slug>.json``, with a small registry file
(``layouts_meta.json``) holding the display names and the last-active slug.

Layout names are free-form display strings; the on-disk filename is the
slugified form. Two defaults (``desktop`` and ``mobile``) always exist as
*names* -- a layout with no saved content file yet is simply "empty", which
the frontend renders as the fresh welcome-chat state.

A legacy single ``layout.json`` (from before named layouts existed) is
migrated on first access: its content becomes the ``desktop`` layout and the
legacy file is renamed aside so the migration runs once.
"""

import json
import re
import threading
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger as _loguru_logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

DESKTOP_LAYOUT_SLUG: Final[str] = "desktop"
MOBILE_LAYOUT_SLUG: Final[str] = "mobile"

_LAYOUTS_SUBDIR: Final[str] = "layouts"
_META_FILENAME: Final[str] = "layouts_meta.json"
_LEGACY_LAYOUT_FILENAME: Final[str] = "layout.json"
_MIGRATED_LEGACY_FILENAME: Final[str] = "layout.json.migrated"

# Serializes every read-modify-write of the meta file + content files across
# the threaded WSGI server. Mirrors the module-level ``_terminal_allocate_lock``
# convention in ``server.py``.
_layouts_lock = threading.Lock()


class LayoutNameError(ValueError):
    """Raised when a layout display name slugifies to nothing usable."""

    ...


class LayoutConflictError(ValueError):
    """Raised when a save's slug collides with a different existing layout."""

    ...


class LayoutNotFoundError(KeyError):
    """Raised when a named layout does not exist in the registry."""

    def __init__(self, slug: str) -> None:
        self.slug = slug
        super().__init__(f"Layout '{slug}' not found")


class LastLayoutDeletionError(ValueError):
    """Raised when deleting a layout would leave the workspace with none."""

    ...


class LayoutInfo(FrozenModel):
    """One named layout as listed to clients."""

    slug: str = Field(description="Slugified filename-safe identifier")
    display_name: str = Field(description="Free-form name shown in the UI")
    has_content: bool = Field(description="Whether a saved content file exists yet")


@pure
def slugify_layout_name(display_name: str) -> str:
    """Project a free-form display name onto its filename-safe slug.

    Raises LayoutNameError when nothing usable remains after slugification.
    """
    lowered = display_name.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    if not slug:
        raise LayoutNameError(f"Layout name {display_name!r} contains no usable characters")
    return slug


def _meta_path(layout_dir: Path) -> Path:
    return layout_dir / _META_FILENAME


def _layouts_dir(layout_dir: Path) -> Path:
    return layout_dir / _LAYOUTS_SUBDIR


def layout_content_path(layout_dir: Path, slug: str) -> Path:
    """On-disk path of one named layout's content file."""
    return _layouts_dir(layout_dir) / f"{slug}.json"


def _default_meta() -> dict[str, Any]:
    return {
        "display_name_by_slug": {
            DESKTOP_LAYOUT_SLUG: DESKTOP_LAYOUT_SLUG,
            MOBILE_LAYOUT_SLUG: MOBILE_LAYOUT_SLUG,
        },
        "last_active_slug": DESKTOP_LAYOUT_SLUG,
    }


def _migrate_legacy_layout_unlocked(layout_dir: Path) -> None:
    """Move a pre-named-layouts ``layout.json`` into the ``desktop`` slot.

    Runs only when the legacy file exists and desktop has no content yet, and
    renames the legacy file aside afterwards so the migration is one-shot.
    """
    legacy_path = layout_dir / _LEGACY_LAYOUT_FILENAME
    if not legacy_path.exists():
        return
    desktop_path = layout_content_path(layout_dir, DESKTOP_LAYOUT_SLUG)
    if not desktop_path.exists():
        desktop_path.parent.mkdir(parents=True, exist_ok=True)
        desktop_path.write_bytes(legacy_path.read_bytes())
        _loguru_logger.info("Migrated legacy layout.json to the '{}' layout", DESKTOP_LAYOUT_SLUG)
    legacy_path.rename(layout_dir / _MIGRATED_LEGACY_FILENAME)


def _read_meta_unlocked(layout_dir: Path) -> dict[str, Any]:
    """Read the registry, initializing defaults + legacy migration on first use.

    A corrupt meta file is treated as first use (logged at warning) rather
    than crashing every layout endpoint: the registry is derivable state and
    the content files themselves are untouched.
    """
    meta_path = _meta_path(layout_dir)
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            _loguru_logger.opt(exception=e).warning("Failed to read {}; reinitializing defaults", meta_path)
            meta = None
        if isinstance(meta, dict) and isinstance(meta.get("display_name_by_slug"), dict):
            return meta
    meta = _default_meta()
    _migrate_legacy_layout_unlocked(layout_dir)
    _write_meta_unlocked(layout_dir, meta)
    return meta


def _write_meta_unlocked(layout_dir: Path, meta: dict[str, Any]) -> None:
    layout_dir.mkdir(parents=True, exist_ok=True)
    _meta_path(layout_dir).write_text(json.dumps(meta, indent=2))


def list_layouts(layout_dir: Path) -> list[LayoutInfo]:
    """Every registered layout, in registry order, with content-presence flags."""
    with _layouts_lock:
        meta = _read_meta_unlocked(layout_dir)
        return [
            LayoutInfo(
                slug=slug,
                display_name=display_name,
                has_content=layout_content_path(layout_dir, slug).exists(),
            )
            for slug, display_name in meta["display_name_by_slug"].items()
        ]


def get_last_active_slug(layout_dir: Path) -> str:
    with _layouts_lock:
        meta = _read_meta_unlocked(layout_dir)
        last_active = meta.get("last_active_slug")
        if isinstance(last_active, str) and last_active in meta["display_name_by_slug"]:
            return last_active
        return next(iter(meta["display_name_by_slug"]))


def set_last_active_slug(layout_dir: Path, slug: str) -> None:
    """Record ``slug`` as the most recently used layout; unknown slugs are ignored."""
    with _layouts_lock:
        meta = _read_meta_unlocked(layout_dir)
        if slug not in meta["display_name_by_slug"]:
            _loguru_logger.warning("Ignored last-active update for unknown layout slug {!r}", slug)
            return
        if meta.get("last_active_slug") != slug:
            meta["last_active_slug"] = slug
            _write_meta_unlocked(layout_dir, meta)


def resolve_layout_slug(layout_dir: Path, name_or_slug: str) -> str:
    """Resolve a display name or slug to a registered slug.

    Raises LayoutNameError on an unusable name and LayoutNotFoundError when
    no registered layout matches.
    """
    slug = slugify_layout_name(name_or_slug)
    with _layouts_lock:
        meta = _read_meta_unlocked(layout_dir)
        if slug not in meta["display_name_by_slug"]:
            raise LayoutNotFoundError(slug)
        return slug


def get_layout_display_name(layout_dir: Path, slug: str) -> str:
    with _layouts_lock:
        meta = _read_meta_unlocked(layout_dir)
        display_name = meta["display_name_by_slug"].get(slug)
        if display_name is None:
            raise LayoutNotFoundError(slug)
        return str(display_name)


def read_layout_content(layout_dir: Path, slug: str) -> dict[str, Any] | None:
    """The saved content of one layout, or None when the layout is still empty.

    Raises LayoutNotFoundError for a slug that is not registered at all.
    A corrupt content file is reported as empty (logged) so the frontend can
    fall back to the fresh-workspace state instead of erroring forever.
    """
    with _layouts_lock:
        meta = _read_meta_unlocked(layout_dir)
        if slug not in meta["display_name_by_slug"]:
            raise LayoutNotFoundError(slug)
        content_path = layout_content_path(layout_dir, slug)
        if not content_path.exists():
            return None
        try:
            content = json.loads(content_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            _loguru_logger.opt(exception=e).warning("Failed to read layout content at {}", content_path)
            return None
        return content if isinstance(content, dict) else None


def write_layout_content(layout_dir: Path, slug: str, content: dict[str, Any]) -> None:
    """Persist ``content`` for an already-registered layout.

    Raises LayoutNotFoundError when the slug is not registered -- autosaves
    against a just-deleted layout must fail rather than resurrect it.
    """
    with _layouts_lock:
        meta = _read_meta_unlocked(layout_dir)
        if slug not in meta["display_name_by_slug"]:
            raise LayoutNotFoundError(slug)
        content_path = layout_content_path(layout_dir, slug)
        content_path.parent.mkdir(parents=True, exist_ok=True)
        content_path.write_text(json.dumps(content, separators=(",", ":")))
        meta["last_active_slug"] = slug
        _write_meta_unlocked(layout_dir, meta)


def register_layout(layout_dir: Path, display_name: str) -> str:
    """Register (or resolve) a layout for ``display_name`` and return its slug.

    An exact display-name match resolves to the existing layout (the
    overwrite path). A slug collision with a *different* display name raises
    LayoutConflictError so two visually-distinct names can never silently
    share one file.
    """
    slug = slugify_layout_name(display_name)
    with _layouts_lock:
        meta = _read_meta_unlocked(layout_dir)
        existing_display_name = meta["display_name_by_slug"].get(slug)
        if existing_display_name is not None and existing_display_name != display_name:
            raise LayoutConflictError(
                f"Layout name {display_name!r} conflicts with existing layout "
                f"{existing_display_name!r} (both shorten to '{slug}')"
            )
        if existing_display_name is None:
            meta["display_name_by_slug"][slug] = display_name
            _write_meta_unlocked(layout_dir, meta)
        return slug


def delete_layout(layout_dir: Path, slug: str) -> str:
    """Delete a layout and return the fallback slug clients should switch to.

    The fallback is the first remaining layout in registry order. Raises
    LayoutNotFoundError for an unknown slug and LastLayoutDeletionError when
    the layout is the only one left.
    """
    with _layouts_lock:
        meta = _read_meta_unlocked(layout_dir)
        if slug not in meta["display_name_by_slug"]:
            raise LayoutNotFoundError(slug)
        if len(meta["display_name_by_slug"]) <= 1:
            raise LastLayoutDeletionError("Cannot delete the last remaining layout")
        del meta["display_name_by_slug"][slug]
        fallback_slug = next(iter(meta["display_name_by_slug"]))
        if meta.get("last_active_slug") == slug:
            meta["last_active_slug"] = fallback_slug
        _write_meta_unlocked(layout_dir, meta)
        content_path = layout_content_path(layout_dir, slug)
        content_path.unlink(missing_ok=True)
        return fallback_slug
