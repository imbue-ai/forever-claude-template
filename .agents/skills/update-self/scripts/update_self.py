#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Deterministic helpers for the safe, background-worker-driven update-self flow.

The update-self orchestration is mostly agent judgement (triage conflicts, decide
validation depth, reveal by change class). This script owns the parts that are
*deterministic* and therefore belong in tested code rather than agent prose:

``resolve-target``
    Resolve the ref to update to. Default is the latest **stable** ``minds-v*``
    tag (semver-sorted, ``-rc``/prerelease excluded); an explicit override may
    name a specific tag, ``main``, or any other ref.

``classify-merge``
    Split the files upstream changed into the reconciled **merged** set (local
    also diverged there -- validate) vs the clean **pulled-in** set (local left
    it untouched, so the merge just took upstream -- trust as upstream-tested),
    and map each file onto its reveal class and its test project. This drives
    both validation depth (merged set) and reveal-by-class.

``trace-consumers``
    Given a changed shared file (a ``scripts/*`` or ``.agents/**`` path), list
    the supervisord programs whose ``command`` references it directly. A shared
    file a live service depends on at runtime must be treated as
    service-impacting (validate + restart), not a silent merge.

``changelog-entries``
    List ``changelog/`` entries newly added between two refs -- the raw input for
    the worker's "what's new" report.

The git-touching subcommands are thin wrappers over the pure functions below
(``pick_latest_stable_tag``, ``resolve_target``, ``classify_path``,
``classify_merge``, ``programs_referencing``), which carry all the logic and are
covered by ``update_self_test.py``.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple, Sequence

# --- Target resolution -----------------------------------------------------

# A released minds version tag, e.g. ``minds-v0.3.7`` (stable) or
# ``minds-v0.3.7-rc1`` (a release candidate -- a prerelease we never default to).
_TAG_RE = re.compile(r"^minds-v(\d+)\.(\d+)\.(\d+)(?:-(?P<pre>.+))?$")


class ResolvedTarget(NamedTuple):
    """The ref the update merges in, plus a coarse ``kind`` for the caller's log.

    ``kind`` is ``tag`` (a resolved ``minds-v*`` release), ``branch`` (``main``),
    or ``ref`` (any other override passed straight through for git to validate).
    """

    ref: str
    kind: str


def _parse_stable_version(tag: str) -> tuple[int, int, int] | None:
    """Return the ``(major, minor, patch)`` of a *stable* ``minds-v*`` tag.

    Returns ``None`` for a non-matching tag or any prerelease (a ``-rc``/``-...``
    suffix), so those never win the "latest stable" selection.
    """
    match = _TAG_RE.match(tag.strip())
    if match is None or match.group("pre") is not None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def pick_latest_stable_tag(tags: Sequence[str]) -> str | None:
    """Return the highest-versioned stable ``minds-v*`` tag, or ``None`` if none.

    Prereleases (``minds-v*-rc*``) and non-matching tags are ignored. Selection is
    by semantic version, not lexical order, so ``minds-v0.3.10`` beats
    ``minds-v0.3.9``.
    """
    stable = [
        (version, tag)
        for tag in tags
        if (version := _parse_stable_version(tag)) is not None
    ]
    if not stable:
        return None
    return max(stable, key=lambda item: item[0])[1]


def resolve_target(
    override: str | None, tags: Sequence[str], remote: str = "upstream"
) -> ResolvedTarget:
    """Resolve the update target ref.

    With no override, pick the latest stable ``minds-v*`` tag (raising if the
    upstream exposes none). An override of ``main`` selects the template's
    default branch, **remote-qualified** to ``<remote>/main`` -- a bare ``main``
    would resolve to the *local* branch, which ``git fetch upstream`` never
    advances, so the pull would merge stale local code. A tag, by contrast,
    lands in the local tag namespace on fetch and resolves by its bare name, so a
    known-tag override is returned as-is. Any other override is passed through
    verbatim as a ``ref`` for git to validate at fetch time (so a user can pin an
    arbitrary commit or a ref they've already qualified themselves).
    """
    if override is None:
        latest = pick_latest_stable_tag(tags)
        if latest is None:
            raise ValueError(
                "no stable minds-v* tag found upstream; pass an explicit "
                "--override (a tag, 'main', or a ref) to update anyway"
            )
        return ResolvedTarget(latest, "tag")
    if override == "main":
        return ResolvedTarget(f"{remote}/{override}", "branch")
    if override in set(tags):
        return ResolvedTarget(override, "tag")
    return ResolvedTarget(override, "ref")


# --- Change classification -------------------------------------------------

CLASS_SYSTEM_INTERFACE = "system_interface"
CLASS_SERVICE = "service"
CLASS_EDITABLE_TOOL = "editable_tool"
CLASS_SHARED_RUNTIME = "shared_runtime"
CLASS_DOCKERFILE = "dockerfile"
CLASS_DOCS = "docs"
CLASS_OTHER = "other"

# Basenames whose change means a dependency manifest moved, so the editable
# install / build needs its env refreshed rather than just picking up new source.
_MANIFEST_BASENAMES = frozenset(
    {"pyproject.toml", "uv.lock", "package.json", "package-lock.json"}
)


class PathClass(NamedTuple):
    """How one changed path should be revealed and validated.

    ``reveal_class`` selects the go-live action; ``project`` is the pytest
    project whose suite covers the path (``.`` = the root workspace,
    ``apps/system_interface`` and ``vendor/mngr`` run their own suites);
    ``is_manifest`` flags a dependency-manifest change that needs an env refresh.
    """

    reveal_class: str
    project: str
    is_manifest: bool


def _project_for_path(path: str) -> str:
    """Return the pytest project root that owns ``path``.

    Only ``apps/system_interface`` and ``vendor/mngr`` carry their own pytest
    config (the root config ignores them); everything else -- libs, scripts,
    ``.agents`` -- is covered by the root suite, reported as ``.``.
    """
    if path.startswith("apps/system_interface/"):
        return "apps/system_interface"
    if path.startswith("vendor/mngr/"):
        return "vendor/mngr"
    return "."


def classify_path(path: str) -> PathClass:
    """Map a repo-relative path to its reveal class, test project, and manifest flag.

    The classes drive reveal-by-class in the skill:

    - ``system_interface`` -- ``apps/system_interface/**``; revealed via
      ``reveal_system_interface.py`` (which owns its own manifest refresh).
    - ``service`` -- ``supervisord.conf`` and ``libs/bootstrap/**``; applied by
      restarting the services agent (``mngr start --restart system-services``).
    - ``editable_tool`` -- ``vendor/mngr/**``; ``.py`` picked up live, a manifest
      change needs ``uv sync --all-packages`` / an editable reinstall.
    - ``shared_runtime`` -- ``scripts/**``, other ``libs/**``, and ``.agents/**``:
      may be a live runtime dependency of a service, so it needs a
      downstream-consumer trace (``trace-consumers``) before it can be called a
      silent merge.
    - ``dockerfile`` -- ``Dockerfile``; split by hunk into live-applicable vs
      rebuild-only by worker judgement.
    - ``docs`` -- ``CLAUDE.md``, ``changelog/**``, and top-level ``*.md``.
    - ``other`` -- anything else.
    """
    is_manifest = Path(path).name in _MANIFEST_BASENAMES
    project = _project_for_path(path)

    if path.startswith("apps/system_interface/"):
        return PathClass(CLASS_SYSTEM_INTERFACE, project, is_manifest)
    if path == "supervisord.conf" or path.startswith("libs/bootstrap/"):
        return PathClass(CLASS_SERVICE, project, is_manifest)
    if path.startswith("vendor/mngr/"):
        return PathClass(CLASS_EDITABLE_TOOL, project, is_manifest)
    if path == "Dockerfile":
        return PathClass(CLASS_DOCKERFILE, project, is_manifest)
    if (
        path.startswith("scripts/")
        or path.startswith(".agents/")
        or path.startswith("libs/")
    ):
        return PathClass(CLASS_SHARED_RUNTIME, project, is_manifest)
    if path == "CLAUDE.md" or path.startswith("changelog/") or path.endswith(".md"):
        return PathClass(CLASS_DOCS, project, is_manifest)
    return PathClass(CLASS_OTHER, project, is_manifest)


class MergeClassification(NamedTuple):
    """The upstream-changed files split by disposition, with per-file class info.

    ``merged`` are files where local also diverged (reconcile + validate);
    ``pulled_in`` are clean upstream arrivals local left untouched (trust, but
    still apply). Each entry is a dict with ``path``, ``reveal_class``,
    ``project``, ``is_manifest``, ``disposition``. The summary fields collect the
    distinct reveal classes and the projects whose suites the merged set implies.
    """

    merged: list[dict[str, object]]
    pulled_in: list[dict[str, object]]
    reveal_classes_merged: list[str]
    reveal_classes_pulled_in: list[str]
    projects_to_validate: list[str]


def _entry(path: str, disposition: str) -> dict[str, object]:
    info = classify_path(path)
    return {
        "path": path,
        "reveal_class": info.reveal_class,
        "project": info.project,
        "is_manifest": info.is_manifest,
        "disposition": disposition,
    }


def classify_merge(
    upstream_changed: Sequence[str], local_changed: Sequence[str]
) -> MergeClassification:
    """Split the upstream-changed files into the merged vs pulled-in sets.

    ``upstream_changed`` is the set of files upstream changed relative to the
    merge base; ``local_changed`` the set the local branch changed relative to
    the same base. A file in both diverged on both sides -> **merged** (validate);
    a file only upstream changed is a clean **pulled-in** arrival (trust). Files
    only *local* changed are not upstream updates at all and are ignored here.
    """
    local = set(local_changed)
    merged: list[dict[str, object]] = []
    pulled_in: list[dict[str, object]] = []
    for path in sorted(set(upstream_changed)):
        if path in local:
            merged.append(_entry(path, "merged"))
        else:
            pulled_in.append(_entry(path, "pulled_in"))

    def _distinct_classes(entries: list[dict[str, object]]) -> list[str]:
        return sorted({str(entry["reveal_class"]) for entry in entries})

    projects = sorted({str(entry["project"]) for entry in merged})
    return MergeClassification(
        merged=merged,
        pulled_in=pulled_in,
        reveal_classes_merged=_distinct_classes(merged),
        reveal_classes_pulled_in=_distinct_classes(pulled_in),
        projects_to_validate=projects,
    )


# --- Downstream-consumer trace ---------------------------------------------

# Any supervisord section header, e.g. ``[program:web]`` or ``[supervisord]``.
_SECTION_RE = re.compile(r"^\[(?P<body>[^\]]+)\]\s*$")
_COMMAND_RE = re.compile(r"^\s*command\s*=\s*(?P<command>.*)$")
# Section types that carry a ``command=`` we should attribute to a live process.
_COMMAND_SECTION_TYPES = frozenset({"program", "eventlistener", "fcgi-program"})


def programs_referencing(path: str, supervisord_text: str) -> list[str]:
    """Return the supervisord programs whose ``command`` references ``path``.

    Parses the ``command=`` line of every command-bearing section
    (``[program:...]``, ``[eventlistener:...]``, ``[fcgi-program:...]``) and
    matches when either the full repo-relative ``path`` or its basename appears
    verbatim in the command string. A hit means a live service shells out to the
    changed file at runtime, so the change is service-impacting (validate +
    restart), not a silent merge. Non-command sections (``[supervisord]``,
    ``[rpcinterface:...]``) reset the current section so their stray settings are
    never misattributed to the preceding program. Transitive or scheduled
    invocations are worker judgement; this catches the deterministic direct
    references.
    """
    basename = Path(path).name
    current: str | None = None
    matches: list[str] = []
    for line in supervisord_text.splitlines():
        section_match = _SECTION_RE.match(line)
        if section_match is not None:
            body = section_match.group("body")
            section_type, _, section_name = body.partition(":")
            # Only command-bearing sections keep a ``current`` name; anything
            # else (a bare ``[supervisord]``, an ``[rpcinterface:...]``) clears
            # it so a later ``command=`` isn't attributed to the wrong program.
            current = (
                section_name if section_type in _COMMAND_SECTION_TYPES else None
            )
            continue
        if current is None:
            continue
        command_match = _COMMAND_RE.match(line)
        if command_match is None:
            continue
        command = command_match.group("command")
        if path in command or basename in command:
            matches.append(current)
    # Preserve first-seen order but drop duplicates (a program has one command,
    # but guard against malformed configs with repeated command lines).
    seen: set[str] = set()
    ordered: list[str] = []
    for name in matches:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


# --- git-touching CLI wrappers ---------------------------------------------


def _git(args: Sequence[str], repo_root: Path) -> str:
    """Run a git command in ``repo_root`` and return its stdout (stripped)."""
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _list_names(output: str) -> list[str]:
    return [line for line in output.splitlines() if line]


def _cmd_resolve_target(args: argparse.Namespace) -> int:
    repo_root = args.repo_root
    tags = _list_names(
        _git(["tag", "--list", "minds-v*"], repo_root)
        if args.local_tags
        else _git(["ls-remote", "--tags", "--refs", args.remote, "minds-v*"], repo_root)
    )
    if not args.local_tags:
        # ``ls-remote`` lines are ``<sha>\trefs/tags/<tag>``; take the tag.
        tags = [line.rsplit("/", 1)[-1] for line in tags]
    target = resolve_target(args.override, tags, remote=args.remote)
    print(json.dumps({"ref": target.ref, "kind": target.kind}))
    return 0


def _cmd_classify_merge(args: argparse.Namespace) -> int:
    repo_root = args.repo_root
    base = args.base or _git(["merge-base", args.local, args.target], repo_root)
    upstream_changed = _list_names(
        _git(["diff", "--name-only", base, args.target], repo_root)
    )
    local_changed = _list_names(
        _git(["diff", "--name-only", base, args.local], repo_root)
    )
    result = classify_merge(upstream_changed, local_changed)
    print(
        json.dumps(
            {
                "base": base,
                "merged": result.merged,
                "pulled_in": result.pulled_in,
                "reveal_classes_merged": result.reveal_classes_merged,
                "reveal_classes_pulled_in": result.reveal_classes_pulled_in,
                "projects_to_validate": result.projects_to_validate,
            },
            indent=2,
        )
    )
    return 0


def _cmd_trace_consumers(args: argparse.Namespace) -> int:
    supervisord = args.supervisord.read_text(encoding="utf-8")
    programs = programs_referencing(args.path, supervisord)
    print(json.dumps({"path": args.path, "programs": programs}))
    return 0


def _cmd_changelog_entries(args: argparse.Namespace) -> int:
    repo_root = args.repo_root
    added = _list_names(
        _git(
            [
                "diff",
                "--name-only",
                "--diff-filter=A",
                args.base,
                args.target,
                "--",
                "changelog/",
            ],
            repo_root,
        )
    )
    print(json.dumps({"added": added}))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    # ``--repo-root`` lives on a shared parent parser so it is accepted both
    # before and after the subcommand (an option defined only on the top-level
    # parser would reject ``update_self.py <subcommand> --repo-root X``).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Repo root the git subcommands run in (default: cwd).",
    )
    parser = argparse.ArgumentParser(description=__doc__, parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)

    resolve_parser = sub.add_parser(
        "resolve-target", help="Resolve the update target ref.", parents=[common]
    )
    resolve_parser.add_argument(
        "--override",
        default=None,
        help="A tag, 'main', or any ref to update to (default: latest stable "
        "minds-v* tag).",
    )
    resolve_parser.add_argument(
        "--remote", default="upstream", help="Remote to read tags from."
    )
    resolve_parser.add_argument(
        "--local-tags",
        action="store_true",
        help="Read already-fetched local tags instead of querying the remote.",
    )
    resolve_parser.set_defaults(func=_cmd_resolve_target)

    classify_parser = sub.add_parser(
        "classify-merge",
        help="Split upstream-changed files into merged vs pulled-in and classify each.",
        parents=[common],
    )
    classify_parser.add_argument(
        "--target", required=True, help="The upstream ref being merged in."
    )
    classify_parser.add_argument(
        "--local",
        default="HEAD",
        help="The local ref (default HEAD; use HEAD^1 after the merge commit).",
    )
    classify_parser.add_argument(
        "--base",
        default=None,
        help="Merge base (default: git merge-base <local> <target>).",
    )
    classify_parser.set_defaults(func=_cmd_classify_merge)

    trace_parser = sub.add_parser(
        "trace-consumers",
        help="List supervisord programs whose command references a changed shared file.",
        parents=[common],
    )
    trace_parser.add_argument("--path", required=True, help="Changed repo-relative path.")
    trace_parser.add_argument(
        "--supervisord",
        type=Path,
        default=Path("supervisord.conf"),
        help="Path to supervisord.conf (default: ./supervisord.conf).",
    )
    trace_parser.set_defaults(func=_cmd_trace_consumers)

    changelog_parser = sub.add_parser(
        "changelog-entries",
        help="List changelog/ entries newly added between two refs.",
        parents=[common],
    )
    changelog_parser.add_argument("--base", required=True, help="Base ref.")
    changelog_parser.add_argument("--target", required=True, help="Target ref.")
    changelog_parser.set_defaults(func=_cmd_changelog_entries)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
