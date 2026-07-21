#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Append entries to the workspace's `VERSION_HISTORY.md` ledger.

`VERSION_HISTORY.md` at the repo root is ONE human-readable record of where this
workspace came from and what it has published -- plain dated lines, each ending
in the commit sha that version was cut from. Two skills write to it, and they
must produce byte-identical lines, so the formatting lives here rather than in
each skill's prose:

- `update-self` appends a `## Workspace` line when it lands a template update.
- `publish-inspiration` appends an `## Inspirations` entry after a successful
  push (`v1`, then `v2`, ... for later updates of the same inspiration).

Design rules this module enforces:

- **Never rewrite an earlier line.** Every operation is an append (or the
  creation of a new `### <slug>` heading); existing lines are copied through
  verbatim, so column alignment of old lines is never re-flowed.
- **Idempotent-safe.** Re-running an append with the same note + sha is a no-op
  (exit 0, "already recorded"), so a retried skill step cannot double-record.
- **Dates are injected.** Every formatting function takes the date as a string;
  the CLI defaults to today only as a convenience, and the skills pass an
  explicit `--date` when they have one.

The creation ("created from") line is seeded from the workspace's creation
snapshot, resolved IN-REPO with no network access, by the same walk
`publish-inspiration` SKILL.md §2 documents for `BASE_REF`: first-parent commits
from HEAD, template-state markers being `update-self: ...` or
`Initial workspace commit`, falling back to the first-parent ROOT (never a bare
root-commit lookup -- subtree merges add parallel roots that are not the seed).
The one deliberate difference is WHICH marker each wants: §2's `BASE_REF` is the
**newest** marker (the base the mind is on *now*), while the ledger's creation
line is the **oldest** one (where the mind started -- every marker after it is an
update, and updates get their own `## Workspace` line). Both are exposed here
(:func:`pick_base_marker`, :func:`pick_creation_marker`); keep them and that
section in step if either changes.
"""

from __future__ import annotations

import argparse
import datetime
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple, Sequence

DEFAULT_FILENAME = "VERSION_HISTORY.md"

WORKSPACE_HEADING = "## Workspace"
INSPIRATIONS_HEADING = "## Inspirations"

# The file shipped in the template: header, the one explanatory line, and the
# two section headings the appends write under.
TEMPLATE_TEXT = """# Version history

Where this workspace came from and what it has published. Entries are appended
automatically -- by `update-self` when it lands a template update, and by
`publish-inspiration` when it publishes -- and earlier lines are never
rewritten. Each line ends in the commit it was cut from.

## Workspace

## Inspirations
"""

# Column widths, chosen so the shas line up in the common case. A longer note
# just pushes its own sha right (the two trailing spaces are always kept);
# earlier lines are never re-flowed to match.
_WORKSPACE_NOTE_WIDTH = 26
_INSPIRATION_NOTE_WIDTH = 35

_CREATED_FROM_PREFIX = "created from "
_UPDATED_TO_PREFIX = "updated to "

# `### <slug>  --  <repo-url>`
_SLUG_HEADING_RE = re.compile(r"^###\s+(?P<slug>\S+)\s+--\s+(?P<url>\S+)\s*$")
# `- v<n>  <date>  <note>  <sha>`
_INSPIRATION_LINE_RE = re.compile(r"^-\s+v(?P<version>\d+)\s")

# A commit subject that marks a template-state snapshot (see the module
# docstring and publish-inspiration SKILL.md §2).
_UPDATE_SELF_SUBJECT_RE = re.compile(r"^update-self:")
_INITIAL_COMMIT_SUBJECT = "Initial workspace commit"


class Commit(NamedTuple):
    """One first-parent commit: its sha and its subject line."""

    sha: str
    subject: str


# --- line rendering ---------------------------------------------------------


def render_workspace_line(date: str, note: str, sha: str) -> str:
    """Render one `## Workspace` line: `- <date>  <note>  <sha>`."""
    return f"- {date}  {note:<{_WORKSPACE_NOTE_WIDTH}}  {sha}".rstrip()


def render_inspiration_line(version: int, date: str, note: str, sha: str) -> str:
    """Render one inspiration line: `- v<n>  <date>  <note>  <sha>`."""
    return f"- v{version}  {date}  {note:<{_INSPIRATION_NOTE_WIDTH}}  {sha}".rstrip()


def render_slug_heading(slug: str, repo_url: str) -> str:
    """Render an inspiration's section heading: `### <slug>  --  <repo-url>`."""
    return f"### {slug}  --  {repo_url}"


# --- document surgery -------------------------------------------------------


def _lines(text: str) -> list[str]:
    return text.splitlines()


def _joined(lines: Sequence[str]) -> str:
    return "\n".join(lines).rstrip("\n") + "\n"


def _section_bounds(lines: Sequence[str], heading: str) -> tuple[int, int] | None:
    """Return `(start, end)` line indices of the body of `heading`.

    `start` is the first line after the heading, `end` is one past the section's
    last line (the next `## ` heading, or end of file). Returns ``None`` when the
    heading is absent.
    """
    start: int | None = None
    for index, line in enumerate(lines):
        if line.strip() == heading:
            start = index + 1
            break
    if start is None:
        return None
    for index in range(start, len(lines)):
        if lines[index].startswith("## "):
            return (start, index)
    return (start, len(lines))


def _ensure_section(lines: list[str], heading: str) -> tuple[int, int]:
    """Return the bounds of `heading`'s body, appending the heading if absent.

    A ledger written by this module always has both headings, but a hand-edited
    or truncated file must not make an append fail: the missing heading is added
    at the end of the file instead.
    """
    bounds = _section_bounds(lines, heading)
    if bounds is not None:
        return bounds
    while lines and not lines[-1].strip():
        lines.pop()
    if lines:
        lines.append("")
    lines.append(heading)
    return (len(lines), len(lines))


def _append_index(lines: Sequence[str], bounds: tuple[int, int]) -> int:
    """The index a new line goes at: after the section body's last content line.

    An empty section body appends directly under its heading, so any blank line
    separating it from the next heading is preserved.
    """
    start, end = bounds
    insert_at = start
    for index in range(start, end):
        if lines[index].strip():
            insert_at = index + 1
    return insert_at


def _section_entries(lines: Sequence[str], bounds: tuple[int, int]) -> list[str]:
    start, end = bounds
    return [line for line in lines[start:end] if line.startswith("- ")]


def has_created_from_line(text: str) -> bool:
    """Whether the `## Workspace` section already carries a "created from" line."""
    lines = _lines(text)
    bounds = _section_bounds(lines, WORKSPACE_HEADING)
    if bounds is None:
        return False
    return any(
        _CREATED_FROM_PREFIX in entry for entry in _section_entries(lines, bounds)
    )


def append_workspace_entry(text: str, date: str, note: str, sha: str) -> str | None:
    """Append a `## Workspace` line, or return ``None`` if it is already recorded.

    "Already recorded" means an existing line in the section carries the same
    note and the same sha -- so a retried skill step is a no-op rather than a
    duplicate.
    """
    lines = _lines(text)
    bounds = _ensure_section(lines, WORKSPACE_HEADING)
    for entry in _section_entries(lines, bounds):
        if note in entry and entry.rstrip().endswith(sha):
            return None
    lines.insert(_append_index(lines, bounds), render_workspace_line(date, note, sha))
    return _joined(lines)


def seed_created_from(text: str, date: str, note: str, sha: str) -> str | None:
    """Seed the "created from" line, or return ``None`` if one already exists.

    Unlike :func:`append_workspace_entry` this is keyed on the *kind* of line,
    not on its content: a workspace has exactly one creation snapshot, so a
    second "created from" line is never correct. The creation line goes FIRST in
    the section (it is the oldest event) -- the only insert in this module that
    is not a plain append, and still never rewrites an existing line.
    """
    if has_created_from_line(text):
        return None
    lines = _lines(text)
    start, _ = _ensure_section(lines, WORKSPACE_HEADING)
    lines.insert(start, render_workspace_line(date, note, sha))
    return _joined(lines)


def _slug_bounds(lines: Sequence[str], slug: str) -> tuple[int, int] | None:
    """Return the body bounds of the `### <slug>` subsection, if present."""
    inspirations = _section_bounds(lines, INSPIRATIONS_HEADING)
    if inspirations is None:
        return None
    section_start, section_end = inspirations
    start: int | None = None
    for index in range(section_start, section_end):
        match = _SLUG_HEADING_RE.match(lines[index].strip())
        if match is not None and match.group("slug") == slug:
            start = index + 1
            break
    if start is None:
        return None
    for index in range(start, section_end):
        if lines[index].startswith("### ") or lines[index].startswith("## "):
            return (start, index)
    return (start, section_end)


def next_version(text: str, slug: str) -> int:
    """Return the version number the next entry for `slug` gets (1 if new)."""
    lines = _lines(text)
    bounds = _slug_bounds(lines, slug)
    if bounds is None:
        return 1
    versions = [
        int(match.group("version"))
        for entry in _section_entries(lines, bounds)
        if (match := _INSPIRATION_LINE_RE.match(entry)) is not None
    ]
    if not versions:
        return 1
    return max(versions) + 1


def append_inspiration_entry(
    text: str, slug: str, repo_url: str, date: str, note: str, sha: str
) -> str | None:
    """Append an `## Inspirations` entry, creating the `### <slug>` heading if new.

    The version number is computed from the entries already under that slug, so
    the first publish is `v1` and every later update is `v(n+1)`. Returns
    ``None`` when an entry with the same note and sha is already recorded.
    """
    lines = _lines(text)
    section_bounds = _ensure_section(lines, INSPIRATIONS_HEADING)
    bounds = _slug_bounds(lines, slug)
    version = next_version(text, slug)
    entry_line = render_inspiration_line(version, date, note, sha)
    if bounds is None:
        insert_at = _append_index(lines, section_bounds)
        block = [render_slug_heading(slug, repo_url), entry_line]
        # Blank line between the section heading (or the previous slug's last
        # entry) and this new subsection.
        if insert_at > 0 and lines[insert_at - 1].strip():
            block.insert(0, "")
        lines[insert_at:insert_at] = block
        return _joined(lines)
    for entry in _section_entries(lines, bounds):
        if note in entry and entry.rstrip().endswith(sha):
            return None
    lines.insert(_append_index(lines, bounds), entry_line)
    return _joined(lines)


# --- creation-snapshot resolution (in-repo, no network) ---------------------


def _is_template_marker(subject: str) -> bool:
    """Whether a commit subject marks a template-state snapshot.

    Two marker kinds, exactly as publish-inspiration SKILL.md §2: an
    `update-self: ...` merge (the mind pulled a newer template) and bootstrap's
    `Initial workspace commit` (the mind's very first boot).
    """
    stripped = subject.strip()
    return bool(_UPDATE_SELF_SUBJECT_RE.match(stripped)) or (
        stripped == _INITIAL_COMMIT_SUBJECT
    )


def pick_base_marker(commits: Sequence[Commit]) -> str | None:
    """Return the NEWEST template-state marker -- §2's `BASE_REF`, or ``None``.

    `commits` are the first-parent commits from HEAD, newest first. This is not
    a judgment call: the newest marker wins, and no older "cleaner-looking"
    template commit past it is ever considered.
    """
    for commit in commits:
        if _is_template_marker(commit.subject):
            return commit.sha
    return None


def pick_creation_marker(commits: Sequence[Commit]) -> str | None:
    """Return the OLDEST template-state marker -- the creation snapshot.

    Same marker set and same first-parent walk as :func:`pick_base_marker`, but
    the ledger's "created from" line records where the workspace *started*
    (normally bootstrap's `Initial workspace commit`); every later marker is an
    update, which gets its own appended `## Workspace` line.
    """
    for commit in reversed(list(commits)):
        if _is_template_marker(commit.subject):
            return commit.sha
    return None


def _git(args: Sequence[str], repo_root: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo_root, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _git_optional(args: Sequence[str], repo_root: Path) -> str | None:
    """`_git` for a query whose "no answer" is a non-zero exit, not an error.

    Only for lookups where absence is a normal outcome (e.g. `git describe` in a
    repo with no matching tag). Everything that must succeed goes through
    :func:`_git` so a broken repo surfaces instead of being swallowed.
    """
    try:
        return _git(args, repo_root)
    except subprocess.CalledProcessError:
        return None


def _first_parent_commits(repo_root: Path) -> list[Commit]:
    output = _git(["log", "--first-parent", "--format=%H %s", "HEAD"], repo_root)
    commits: list[Commit] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        sha, _, subject = line.partition(" ")
        commits.append(Commit(sha, subject))
    return commits


def _first_parent_root(repo_root: Path) -> str:
    """The fallback when no marker exists: the FIRST-PARENT root commit."""
    roots = _git(["rev-list", "--first-parent", "HEAD"], repo_root).splitlines()
    if not roots:
        raise ValueError("cannot resolve a template-state commit: HEAD has no history")
    return roots[-1].strip()


def resolve_creation_snapshot(repo_root: Path) -> str:
    """Resolve this workspace's creation snapshot commit (full sha)."""
    marker = pick_creation_marker(_first_parent_commits(repo_root))
    return marker if marker is not None else _first_parent_root(repo_root)


def resolve_base_ref(repo_root: Path) -> str:
    """Resolve the base the mind is on NOW -- §2's `BASE_REF` (full sha)."""
    marker = pick_base_marker(_first_parent_commits(repo_root))
    return marker if marker is not None else _first_parent_root(repo_root)


def _short_sha(repo_root: Path, ref: str) -> str:
    return _git(["rev-parse", "--short=7", ref], repo_root)


def _commit_date(repo_root: Path, ref: str) -> str:
    return _git(["log", "-1", "--format=%ad", "--date=short", ref], repo_root)


def _template_version_at(repo_root: Path, ref: str) -> str | None:
    """The nearest `minds-v*` tag REACHABLE FROM `ref`, if the repo has one.

    Reachable, not pointing-at: no tag ever sits on a creation snapshot. The
    `Initial workspace commit` marker is an `--allow-empty` commit bootstrap
    writes ON TOP of the cloned template commit, and an `update-self:` marker is
    a merge commit -- in both cases the `minds-v*` tag is on an ancestor. A
    pointing-at lookup would therefore always come up empty and every creation
    line would degrade to the unnamed fallback.
    """
    return _git_optional(
        ["describe", "--tags", "--abbrev=0", "--match", "minds-v*", ref], repo_root
    )


# --- CLI --------------------------------------------------------------------


def _repo_root(args: argparse.Namespace) -> Path:
    """The ``--repo-root`` value, whether given before or after the subcommand.

    The attribute is absent (not defaulted) when the flag was never passed --
    see the ``SUPPRESS`` note in :func:`main` -- so the cwd fallback lives here.
    """
    return Path(getattr(args, "repo_root", "."))


def _ledger_path(args: argparse.Namespace) -> Path:
    return _repo_root(args) / getattr(args, "file", DEFAULT_FILENAME)


def _read(path: Path) -> str:
    if not path.is_file():
        return TEMPLATE_TEXT
    return path.read_text(encoding="utf-8")


def _write_result(path: Path, updated: str | None, entry_kind: str) -> int:
    """Write `updated` (or report the no-op) and return the process exit code."""
    if updated is None:
        print(f"{entry_kind}: already recorded, nothing to do")
        return 0
    path.write_text(updated, encoding="utf-8")
    print(f"{entry_kind}: recorded in {path}")
    return 0


def _seed(
    repo_root: Path,
    text: str,
    template_version: str | None = None,
    date: str | None = None,
) -> str | None:
    """Return the text with the "created from" line seeded, or ``None`` if present.

    The date and the template version are resolved from the creation commit
    itself (its commit date; the nearest `minds-v*` tag it can reach) rather than
    from the caller's clock, so seeding late -- the first time any skill writes
    to the ledger -- still records when the workspace was actually created.
    """
    if has_created_from_line(text):
        return None
    creation_sha = resolve_creation_snapshot(repo_root)
    version = template_version or _template_version_at(repo_root, creation_sha)
    note = (
        f"{_CREATED_FROM_PREFIX}{version}"
        if version
        else "created from the workspace template"
    )
    return seed_created_from(
        text,
        date or _commit_date(repo_root, creation_sha),
        note,
        _short_sha(repo_root, creation_sha),
    )


def _cmd_init(args: argparse.Namespace) -> int:
    path = _ledger_path(args)
    if path.is_file() and not args.force:
        print(f"version history: {path} already exists, leaving it alone")
        return 0
    path.write_text(TEMPLATE_TEXT, encoding="utf-8")
    print(f"version history: wrote the template ledger to {path}")
    return 0


def _cmd_seed_workspace(args: argparse.Namespace) -> int:
    path = _ledger_path(args)
    seeded = _seed(_repo_root(args), _read(path), args.template_version, args.date)
    return _write_result(path, seeded, "workspace creation line")


def _cmd_add_workspace(args: argparse.Namespace) -> int:
    path = _ledger_path(args)
    repo_root = _repo_root(args)
    # Seeding first means a workspace that never had a ledger still records
    # where it came from before recording what it updated to.
    text = _read(path)
    seeded = _seed(repo_root, text)
    date = args.date or datetime.date.today().isoformat()
    updated = append_workspace_entry(
        seeded if seeded is not None else text,
        date,
        f"{_UPDATED_TO_PREFIX}{args.template_version}",
        _short_sha(repo_root, args.sha),
    )
    return _write_result(path, updated or seeded, "workspace update line")


def _cmd_add_inspiration(args: argparse.Namespace) -> int:
    path = _ledger_path(args)
    repo_root = _repo_root(args)
    text = _read(path)
    seeded = _seed(repo_root, text)
    date = args.date or datetime.date.today().isoformat()
    updated = append_inspiration_entry(
        seeded if seeded is not None else text,
        args.slug,
        args.repo_url,
        date,
        args.note,
        _short_sha(repo_root, args.sha),
    )
    return _write_result(path, updated or seeded, "inspiration entry")


def _cmd_resolve_base_ref(args: argparse.Namespace) -> int:
    print(resolve_base_ref(_repo_root(args)))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    # ``--repo-root`` / ``--file`` live on a shared parent parser so they are
    # accepted both before and after the subcommand. Their default must be
    # ``SUPPRESS``, not a value: on Python < 3.13 a subparser re-applies its
    # defaults over the namespace the top-level parser already filled in
    # (bpo-9351), so a concrete default here would clobber a value given
    # *before* the subcommand. With ``SUPPRESS`` the attribute is only set when
    # the flag is actually passed, and ``_repo_root`` / ``_ledger_path`` supply
    # the fallbacks.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--repo-root",
        default=argparse.SUPPRESS,
        help="Repo root holding the ledger (default: cwd).",
    )
    common.add_argument(
        "--file",
        default=argparse.SUPPRESS,
        help=f"Ledger filename, relative to the repo root (default: {DEFAULT_FILENAME}).",
    )

    parser = argparse.ArgumentParser(description=__doc__, parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser(
        "init",
        help="Write the template ledger (used when none exists).",
        parents=[common],
    )
    init_parser.add_argument(
        "--force", action="store_true", help="Overwrite an existing ledger."
    )
    init_parser.set_defaults(func=_cmd_init)

    seed_parser = sub.add_parser(
        "seed-workspace",
        help="Seed the `created from` line from this repo's creation snapshot.",
        parents=[common],
    )
    seed_parser.add_argument(
        "--template-version",
        default=None,
        help="Template version created from (default: the nearest minds-v* tag "
        "reachable from the creation snapshot, else unnamed).",
    )
    seed_parser.add_argument(
        "--date",
        default=None,
        help="YYYY-MM-DD (default: the creation snapshot's commit date).",
    )
    seed_parser.set_defaults(func=_cmd_seed_workspace)

    add_workspace_parser = sub.add_parser(
        "add-workspace",
        help="Append a `## Workspace` line for a landed template update.",
        parents=[common],
    )
    add_workspace_parser.add_argument(
        "--template-version", required=True, help="The template version updated to."
    )
    add_workspace_parser.add_argument(
        "--sha", required=True, help="The merge commit (any rev; recorded short)."
    )
    add_workspace_parser.add_argument(
        "--date", default=None, help="YYYY-MM-DD (default: today)."
    )
    add_workspace_parser.set_defaults(func=_cmd_add_workspace)

    add_inspiration_parser = sub.add_parser(
        "add-inspiration",
        help="Append an `## Inspirations` entry (v1, then v2, ... per slug).",
        parents=[common],
    )
    add_inspiration_parser.add_argument("--slug", required=True)
    add_inspiration_parser.add_argument(
        "--repo-url", required=True, help="e.g. github.com/<owner>/<repo>."
    )
    add_inspiration_parser.add_argument(
        "--note", required=True, help="One short line: what this version is."
    )
    add_inspiration_parser.add_argument(
        "--sha",
        required=True,
        help="The SOURCE workspace commit the snapshot was cut from.",
    )
    add_inspiration_parser.add_argument(
        "--date", default=None, help="YYYY-MM-DD (default: today)."
    )
    add_inspiration_parser.set_defaults(func=_cmd_add_inspiration)

    resolve_parser = sub.add_parser(
        "resolve-base-ref",
        help="Print the newest template-state commit (publish-inspiration §2's "
        "BASE_REF), resolved in-repo with no network.",
        parents=[common],
    )
    resolve_parser.set_defaults(func=_cmd_resolve_base_ref)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, subprocess.CalledProcessError) as exc:
        print(f"version history: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
