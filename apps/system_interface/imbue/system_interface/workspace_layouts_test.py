import json
from pathlib import Path

import pytest

from imbue.system_interface.workspace_layouts import DESKTOP_LAYOUT_SLUG
from imbue.system_interface.workspace_layouts import LastLayoutDeletionError
from imbue.system_interface.workspace_layouts import LayoutConflictError
from imbue.system_interface.workspace_layouts import LayoutNameError
from imbue.system_interface.workspace_layouts import LayoutNotFoundError
from imbue.system_interface.workspace_layouts import MOBILE_LAYOUT_SLUG
from imbue.system_interface.workspace_layouts import delete_layout
from imbue.system_interface.workspace_layouts import get_last_active_slug
from imbue.system_interface.workspace_layouts import layout_content_path
from imbue.system_interface.workspace_layouts import list_layouts
from imbue.system_interface.workspace_layouts import read_layout_content
from imbue.system_interface.workspace_layouts import register_layout
from imbue.system_interface.workspace_layouts import resolve_layout_slug
from imbue.system_interface.workspace_layouts import set_last_active_slug
from imbue.system_interface.workspace_layouts import slugify_layout_name
from imbue.system_interface.workspace_layouts import write_layout_content


def test_slugify_layout_name_normalizes() -> None:
    assert slugify_layout_name("My Fancy Setup!") == "my-fancy-setup"
    assert slugify_layout_name("  desktop  ") == "desktop"
    assert slugify_layout_name("a_b c") == "a-b-c"


def test_slugify_layout_name_rejects_unusable() -> None:
    with pytest.raises(LayoutNameError):
        slugify_layout_name("!!!")
    with pytest.raises(LayoutNameError):
        slugify_layout_name("   ")


def test_defaults_initialize_on_first_access(tmp_path: Path) -> None:
    infos = list_layouts(tmp_path)
    assert [info.slug for info in infos] == [DESKTOP_LAYOUT_SLUG, MOBILE_LAYOUT_SLUG]
    assert all(info.has_content is False for info in infos)
    assert get_last_active_slug(tmp_path) == DESKTOP_LAYOUT_SLUG


def test_legacy_layout_migrates_to_desktop_once(tmp_path: Path) -> None:
    legacy_content = {"dockview": {"panels": {}}, "panelParams": {}}
    (tmp_path / "layout.json").write_text(json.dumps(legacy_content))

    assert read_layout_content(tmp_path, DESKTOP_LAYOUT_SLUG) == legacy_content
    assert not (tmp_path / "layout.json").exists()
    assert (tmp_path / "layout.json.migrated").exists()
    # Mobile is unaffected by the migration.
    assert read_layout_content(tmp_path, MOBILE_LAYOUT_SLUG) is None


def test_write_and_read_round_trip(tmp_path: Path) -> None:
    content = {"dockview": {"grid": {}}, "panelParams": {"p": {"panelType": "chat"}}}
    write_layout_content(tmp_path, MOBILE_LAYOUT_SLUG, content)
    assert read_layout_content(tmp_path, MOBILE_LAYOUT_SLUG) == content
    assert layout_content_path(tmp_path, MOBILE_LAYOUT_SLUG).exists()
    # Writing marks the layout as the most recently used one.
    assert get_last_active_slug(tmp_path) == MOBILE_LAYOUT_SLUG


def test_write_unknown_layout_raises(tmp_path: Path) -> None:
    with pytest.raises(LayoutNotFoundError):
        write_layout_content(tmp_path, "ghost", {})


def test_register_layout_resolves_exact_display_name(tmp_path: Path) -> None:
    slug = register_layout(tmp_path, "My Setup")
    assert slug == "my-setup"
    # Registering the same display name again resolves to the same slug.
    assert register_layout(tmp_path, "My Setup") == "my-setup"


def test_register_layout_rejects_slug_collision(tmp_path: Path) -> None:
    register_layout(tmp_path, "My Setup")
    with pytest.raises(LayoutConflictError):
        register_layout(tmp_path, "my setup")


def test_resolve_layout_slug_accepts_display_name(tmp_path: Path) -> None:
    register_layout(tmp_path, "My Setup")
    assert resolve_layout_slug(tmp_path, "My Setup") == "my-setup"
    assert resolve_layout_slug(tmp_path, "my-setup") == "my-setup"
    with pytest.raises(LayoutNotFoundError):
        resolve_layout_slug(tmp_path, "unknown")


def test_delete_layout_returns_fallback_and_guards_last(tmp_path: Path) -> None:
    write_layout_content(tmp_path, MOBILE_LAYOUT_SLUG, {"x": 1})
    set_last_active_slug(tmp_path, MOBILE_LAYOUT_SLUG)

    fallback = delete_layout(tmp_path, MOBILE_LAYOUT_SLUG)
    assert fallback == DESKTOP_LAYOUT_SLUG
    # The content file is gone, the registry no longer lists it, and the
    # last-active pointer moved off the deleted layout.
    assert not layout_content_path(tmp_path, MOBILE_LAYOUT_SLUG).exists()
    assert [info.slug for info in list_layouts(tmp_path)] == [DESKTOP_LAYOUT_SLUG]
    assert get_last_active_slug(tmp_path) == DESKTOP_LAYOUT_SLUG

    with pytest.raises(LastLayoutDeletionError):
        delete_layout(tmp_path, DESKTOP_LAYOUT_SLUG)
    with pytest.raises(LayoutNotFoundError):
        delete_layout(tmp_path, MOBILE_LAYOUT_SLUG)


def test_set_last_active_ignores_unknown_slug(tmp_path: Path) -> None:
    set_last_active_slug(tmp_path, "ghost")
    assert get_last_active_slug(tmp_path) == DESKTOP_LAYOUT_SLUG


def test_corrupt_meta_reinitializes_defaults(tmp_path: Path) -> None:
    list_layouts(tmp_path)
    (tmp_path / "layouts_meta.json").write_text("not json{")
    infos = list_layouts(tmp_path)
    assert [info.slug for info in infos] == [DESKTOP_LAYOUT_SLUG, MOBILE_LAYOUT_SLUG]


def test_corrupt_content_reads_as_empty(tmp_path: Path) -> None:
    write_layout_content(tmp_path, DESKTOP_LAYOUT_SLUG, {"ok": True})
    layout_content_path(tmp_path, DESKTOP_LAYOUT_SLUG).write_text("garbage{")
    assert read_layout_content(tmp_path, DESKTOP_LAYOUT_SLUG) is None
