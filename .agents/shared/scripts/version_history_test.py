"""Unit tests for the shared VERSION_HISTORY.md helper.

Covers what both writing skills depend on being exactly right: the byte-level
line format (they must produce identical lines), append-only behavior, the
per-slug version counter, idempotent re-runs, and the in-repo creation-snapshot
resolution (newest vs oldest template-state marker, first-parent-root fallback).
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).with_name("version_history.py")
_spec = importlib.util.spec_from_file_location("version_history", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
version_history = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(version_history)


# --- line format -----------------------------------------------------------


def test_rendered_lines_match_the_documented_format() -> None:
    # These are the exact lines the design doc specifies; both skills go
    # through these renderers so their output cannot drift apart.
    assert (
        version_history.render_workspace_line(
            "2026-05-02", "created from minds-v0.3.6", "a1b2c3d"
        )
        == "- 2026-05-02  created from minds-v0.3.6   a1b2c3d"
    )
    assert (
        version_history.render_workspace_line(
            "2026-07-20", "updated to minds-v0.3.8", "9f8e7d6"
        )
        == "- 2026-07-20  updated to minds-v0.3.8     9f8e7d6"
    )
    assert (
        version_history.render_inspiration_line(
            1, "2026-06-15", "first published", "c0ffee1"
        )
        == "- v1  2026-06-15  first published                      c0ffee1"
    )
    assert (
        version_history.render_inspiration_line(
            2, "2026-07-25", "synced the bug fix in sync.py", "deadbee"
        )
        == "- v2  2026-07-25  synced the bug fix in sync.py        deadbee"
    )


def test_an_overlong_note_still_keeps_two_spaces_before_the_sha() -> None:
    # Padding is a minimum, not a truncation: a long note pushes its own sha
    # right rather than colliding with it or losing the separator.
    line = version_history.render_workspace_line(
        "2026-07-20", "updated to a very long template version name", "9f8e7d6"
    )
    assert line.endswith("name  9f8e7d6")


# --- workspace entries ------------------------------------------------------


def test_seed_created_from_goes_first_and_is_idempotent() -> None:
    seeded = version_history.seed_created_from(
        version_history.TEMPLATE_TEXT,
        "2026-05-02",
        "created from minds-v0.3.6",
        "a1b2c3d",
    )
    assert seeded is not None
    workspace_index = seeded.index(version_history.WORKSPACE_HEADING)
    assert seeded[workspace_index:].startswith(
        "## Workspace\n- 2026-05-02  created from minds-v0.3.6   a1b2c3d\n\n## Inspirations"
    )
    # A workspace has exactly one creation snapshot -- a second seed is a no-op
    # even with different values.
    assert (
        version_history.seed_created_from(
            seeded, "2026-06-01", "created from minds-v0.3.9", "ffffff1"
        )
        is None
    )


def test_append_workspace_entry_appends_after_the_creation_line() -> None:
    text = version_history.seed_created_from(
        version_history.TEMPLATE_TEXT,
        "2026-05-02",
        "created from minds-v0.3.6",
        "a1b2c3d",
    )
    assert text is not None
    updated = version_history.append_workspace_entry(
        text, "2026-07-20", "updated to minds-v0.3.8", "9f8e7d6"
    )
    assert updated is not None
    assert (
        "## Workspace\n"
        "- 2026-05-02  created from minds-v0.3.6   a1b2c3d\n"
        "- 2026-07-20  updated to minds-v0.3.8     9f8e7d6\n"
    ) in updated
    # Earlier lines are copied through verbatim, never re-flowed.
    assert "- 2026-05-02  created from minds-v0.3.6   a1b2c3d" in updated


def test_append_workspace_entry_is_idempotent_on_the_same_note_and_sha() -> None:
    once = version_history.append_workspace_entry(
        version_history.TEMPLATE_TEXT,
        "2026-07-20",
        "updated to minds-v0.3.8",
        "9f8e7d6",
    )
    assert once is not None
    assert (
        version_history.append_workspace_entry(
            once, "2026-07-21", "updated to minds-v0.3.8", "9f8e7d6"
        )
        is None
    )
    # A different sha is a different update and does get recorded.
    twice = version_history.append_workspace_entry(
        once, "2026-08-01", "updated to minds-v0.3.9", "1234567"
    )
    assert twice is not None
    assert twice.count("- 2026") == 2


# --- inspiration entries ----------------------------------------------------


def test_append_inspiration_creates_the_slug_heading_then_appends_versions() -> None:
    first = version_history.append_inspiration_entry(
        version_history.TEMPLATE_TEXT,
        "people-crm",
        "github.com/preston/people-crm",
        "2026-06-15",
        "first published",
        "c0ffee1",
    )
    assert first is not None
    assert (
        "## Inspirations\n\n"
        "### people-crm  --  github.com/preston/people-crm\n"
        "- v1  2026-06-15  first published                      c0ffee1\n"
    ) in first

    second = version_history.append_inspiration_entry(
        first,
        "people-crm",
        "github.com/preston/people-crm",
        "2026-07-25",
        "synced the bug fix in sync.py",
        "deadbee",
    )
    assert second is not None
    assert second.count("### people-crm") == 1
    assert (
        "- v1  2026-06-15  first published                      c0ffee1\n"
        "- v2  2026-07-25  synced the bug fix in sync.py        deadbee\n"
    ) in second


def test_inspiration_versions_are_per_slug() -> None:
    text = version_history.append_inspiration_entry(
        version_history.TEMPLATE_TEXT,
        "people-crm",
        "github.com/preston/people-crm",
        "2026-06-15",
        "first published",
        "c0ffee1",
    )
    assert text is not None
    text = version_history.append_inspiration_entry(
        text,
        "people-crm",
        "github.com/preston/people-crm",
        "2026-07-25",
        "synced sync.py",
        "deadbee",
    )
    assert text is not None
    text = version_history.append_inspiration_entry(
        text,
        "slack-inbox",
        "github.com/preston/slack-inbox",
        "2026-07-26",
        "first published",
        "abcdef1",
    )
    assert text is not None
    assert version_history.next_version(text, "people-crm") == 3
    assert version_history.next_version(text, "slack-inbox") == 2
    assert version_history.next_version(text, "never-published") == 1
    # The second slug gets its own heading, separated by a blank line, and the
    # first slug's entries are untouched.
    assert (
        "- v2  2026-07-25  synced sync.py                       deadbee\n"
        "\n"
        "### slack-inbox  --  github.com/preston/slack-inbox\n"
        "- v1  2026-07-26  first published                      abcdef1\n"
    ) in text


def test_append_inspiration_is_idempotent_on_the_same_note_and_sha() -> None:
    once = version_history.append_inspiration_entry(
        version_history.TEMPLATE_TEXT,
        "people-crm",
        "github.com/preston/people-crm",
        "2026-06-15",
        "first published",
        "c0ffee1",
    )
    assert once is not None
    assert (
        version_history.append_inspiration_entry(
            once,
            "people-crm",
            "github.com/preston/people-crm",
            "2026-06-16",
            "first published",
            "c0ffee1",
        )
        is None
    )


def test_missing_sections_are_created_rather_than_failing() -> None:
    # A hand-edited or truncated ledger must not make an append fail.
    updated = version_history.append_inspiration_entry(
        "# Version history\n",
        "x",
        "github.com/a/x",
        "2026-06-15",
        "first published",
        "c0ffee1",
    )
    assert updated is not None
    assert "## Inspirations" in updated
    assert "- v1  2026-06-15" in updated


# --- creation-snapshot resolution -------------------------------------------


def _commits(*subjects: str) -> list[version_history.Commit]:
    """Newest-first first-parent commits, shas derived from the position."""
    return [
        version_history.Commit(f"sha{index}", subject)
        for index, subject in enumerate(subjects)
    ]


def test_base_marker_is_newest_and_creation_marker_is_oldest() -> None:
    commits = _commits(
        "some app work",
        "update-self: merge upstream template (minds-v0.3.8)",
        "more app work",
        "update-self: merge upstream template (minds-v0.3.7)",
        "Initial workspace commit",
    )
    # publish-inspiration §2's BASE_REF: the newest marker (the base the mind
    # is on now).
    assert version_history.pick_base_marker(commits) == "sha1"
    # The ledger's creation line: where the workspace started.
    assert version_history.pick_creation_marker(commits) == "sha4"


def test_markers_return_none_when_absent() -> None:
    commits = _commits("app work", "more app work")
    assert version_history.pick_base_marker(commits) is None
    assert version_history.pick_creation_marker(commits) is None


def _git(repo: Path, *args: str) -> str:
    """Run one git command in `repo` and return its stdout."""
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _init_bare_repo(repo: Path) -> None:
    """An empty git repo with a committer configured -- no commits yet."""
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")


def _init_repo(repo: Path) -> None:
    """A minimal workspace repo: one `Initial workspace commit` marker."""
    _init_bare_repo(repo)
    _git(repo, "commit", "--allow-empty", "-q", "-m", "Initial workspace commit")


def test_resolution_falls_back_to_the_first_parent_root(tmp_path: Path) -> None:
    _init_bare_repo(tmp_path)
    _git(tmp_path, "commit", "--allow-empty", "-q", "-m", "root")
    root = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "commit", "--allow-empty", "-q", "-m", "app work")

    # No marker anywhere -> both resolutions fall back to the first-parent root.
    assert version_history.resolve_creation_snapshot(tmp_path) == root
    assert version_history.resolve_base_ref(tmp_path) == root

    _git(tmp_path, "commit", "--allow-empty", "-q", "-m", "Initial workspace commit")
    initial = _git(tmp_path, "rev-parse", "HEAD")
    _git(
        tmp_path,
        "commit",
        "--allow-empty",
        "-q",
        "-m",
        "update-self: merge upstream template",
    )
    update = _git(tmp_path, "rev-parse", "HEAD")
    assert version_history.resolve_creation_snapshot(tmp_path) == initial
    assert version_history.resolve_base_ref(tmp_path) == update


# --- CLI --------------------------------------------------------------------


def test_cli_seeds_the_creation_line_before_recording_a_publish(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    exit_code = version_history.main(
        [
            "--repo-root",
            str(tmp_path),
            "add-inspiration",
            "--slug",
            "people-crm",
            "--repo-url",
            "github.com/preston/people-crm",
            "--note",
            "first published",
            "--sha",
            "HEAD",
            "--date",
            "2026-06-15",
        ]
    )
    assert exit_code == 0
    text = (tmp_path / version_history.DEFAULT_FILENAME).read_text(encoding="utf-8")
    # The ledger did not exist: it was created from the template, seeded with
    # the creation line (dated from the creation commit, not today), and given
    # the v1 entry.
    assert "created from the workspace template" in text
    assert "- v1  2026-06-15  first published" in text
    assert "### people-crm  --  github.com/preston/people-crm" in text


def test_cli_names_the_template_version_the_creation_snapshot_can_reach(
    tmp_path: Path,
) -> None:
    # The real topology: the `minds-v*` tag is on the TEMPLATE commit, and
    # bootstrap's `Initial workspace commit` marker sits on top of it untagged.
    # The creation line must still name the version -- a tag-points-at lookup
    # would find nothing here and silently degrade to the unnamed fallback.
    _init_bare_repo(tmp_path)
    _git(tmp_path, "commit", "--allow-empty", "-q", "-m", "template release")
    _git(tmp_path, "tag", "minds-v0.3.6")
    _git(tmp_path, "commit", "--allow-empty", "-q", "-m", "Initial workspace commit")
    _git(tmp_path, "commit", "--allow-empty", "-q", "-m", "app work")

    assert version_history.main(["--repo-root", str(tmp_path), "seed-workspace"]) == 0
    text = (tmp_path / version_history.DEFAULT_FILENAME).read_text(encoding="utf-8")
    assert "created from minds-v0.3.6" in text


def test_cli_add_workspace_is_idempotent(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    argv = [
        "--repo-root",
        str(tmp_path),
        "add-workspace",
        "--template-version",
        "minds-v0.3.8",
        "--sha",
        "HEAD",
        "--date",
        "2026-07-20",
    ]
    assert version_history.main(argv) == 0
    assert version_history.main(argv) == 0
    text = (tmp_path / version_history.DEFAULT_FILENAME).read_text(encoding="utf-8")
    assert text.count("updated to minds-v0.3.8") == 1


def test_cli_accepts_repo_root_after_the_subcommand(tmp_path: Path) -> None:
    # The shared flags default to argparse.SUPPRESS precisely so either
    # position works; every other test passes them first, so cover the other.
    _init_repo(tmp_path)
    assert (
        version_history.main(
            [
                "add-workspace",
                "--template-version",
                "minds-v0.3.8",
                "--sha",
                "HEAD",
                "--repo-root",
                str(tmp_path),
            ]
        )
        == 0
    )
    ledger = tmp_path / version_history.DEFAULT_FILENAME
    assert ledger.is_file(), "the ledger went somewhere other than --repo-root"
    assert "updated to minds-v0.3.8" in ledger.read_text(encoding="utf-8")


def test_cli_resolve_base_ref_prints_the_marker_and_keeps_printing_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_repo(tmp_path)
    _git(tmp_path, "commit", "--allow-empty", "-q", "-m", "update-self: merge upstream")
    merge = _git(tmp_path, "rev-parse", "HEAD")

    def _resolved() -> str:
        assert (
            version_history.main(["--repo-root", str(tmp_path), "resolve-base-ref"]) == 0
        )
        return capsys.readouterr().out.strip()

    assert _resolved() == merge
    # Commits landing on top do not move the answer -- update-self's landing
    # step passes this sha to `add-workspace` and then commits the ledger, so a
    # re-run has to resolve to the same merge for the append to stay a no-op.
    _git(tmp_path, "commit", "--allow-empty", "-q", "-m", "version history: updated")
    assert _resolved() == merge


def test_shipped_ledger_matches_the_template() -> None:
    # The file shipped at the repo root and the text this module writes when a
    # ledger is missing must be the same thing, or a workspace that seeds late
    # gets a different header than one that shipped with it.
    repo_root = Path(__file__).resolve().parents[3]
    shipped = repo_root / version_history.DEFAULT_FILENAME
    assert shipped.is_file(), f"{shipped} is missing"
    assert shipped.read_text(encoding="utf-8") == version_history.TEMPLATE_TEXT


def test_cli_init_does_not_clobber_an_existing_ledger(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    ledger = tmp_path / version_history.DEFAULT_FILENAME
    ledger.write_text("# Version history\n\n## Workspace\n- kept\n", encoding="utf-8")
    assert version_history.main(["--repo-root", str(tmp_path), "init"]) == 0
    assert "- kept" in ledger.read_text(encoding="utf-8")
    assert version_history.main(["--repo-root", str(tmp_path), "init", "--force"]) == 0
    assert ledger.read_text(encoding="utf-8") == version_history.TEMPLATE_TEXT
