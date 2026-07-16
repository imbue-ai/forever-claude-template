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

``changelog-entries``
    List ``changelog/`` entries newly added between two refs -- the raw input for
    the worker's "what's new" report.

``bootstrap-skill``
    Extract the target ref's *own* copy of the update-self skill (SKILL.md,
    references, scripts) into a staging dir and report whether it differs from the
    local copy. This is what lets the flow, after resolving the target, hand off
    to the update-self process *as it exists at the version being updated to* --
    so fixes to the update flow itself are applied live rather than being gated on
    the possibly-stale local copy.

Impact analysis -- which services and skills depend on a changed file -- is
deliberately NOT scripted here: it requires open-ended exploration (imports,
shelled-out scripts, API-surface coupling) that a deterministic helper would
only pretend to cover. The worker reference owns that recipe.

The git-touching subcommands are thin wrappers over the pure functions below
(``pick_latest_stable_tag``, ``resolve_target``, ``classify_path``,
``classify_merge``), which carry all the logic and are covered by
``update_self_test.py``.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import NamedTuple, Sequence

# The repo-relative directory holding the update-self skill (SKILL.md,
# references/, scripts/). Used by ``bootstrap-skill`` to extract the target
# ref's own copy of the flow.
SKILL_DIR_REL = ".agents/skills/update-self"

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
CLASS_PROVISIONER = "provisioner"
CLASS_DOCKERFILE = "dockerfile"
CLASS_DOCS = "docs"
CLASS_OTHER = "other"

# Files whose effects land at image-build / workspace-create / first-boot
# provisioning time -- the pinned global toolchain and the create/agent config --
# rather than at runtime. A change to one never reaches a *live* workspace by
# restarting a service (nothing running imports it): it needs the provisioning
# step re-run live (these scripts are idempotent) or a workspace rebuild. Split
# out of ``shared_runtime``/``other`` so the reveal can flag that downstream
# impact instead of concluding "nothing to reveal". See the skill's
# ``provisioner`` reveal class.
_PROVISIONER_SCRIPTS = frozenset(
    {
        "scripts/setup_system.sh",  # pinned global toolchain (latchkey, uv, claude, ...)
        "scripts/install_secret_scanners.sh",  # pinned global scanner binaries
        "scripts/_provision_guard.sh",  # the guard that gates the above
    }
)


def _is_provisioner(path: str) -> bool:
    """Whether ``path`` shapes how the workspace/agent is *provisioned*.

    The pinned-toolchain scripts (:data:`_PROVISIONER_SCRIPTS`) plus everything
    under ``.mngr/`` -- the ``mngr create`` defaults, provider blocks, and the
    agent Claude-version pin that provisioning applies to every new workspace.
    """
    return path in _PROVISIONER_SCRIPTS or path.startswith(".mngr/")


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
      may be a live runtime dependency of a service or a workspace-added skill,
      so it needs the worker's impact analysis before it can be called a silent
      merge.
    - ``provisioner`` -- the pinned-toolchain scripts and the ``.mngr/`` create
      config (see :func:`_is_provisioner`); shapes image-build / create-time
      provisioning, so a change is re-run live (idempotent scripts) or flagged
      for a workspace rebuild, never revealed by a service restart.
    - ``dockerfile`` -- ``Dockerfile``; split by hunk into live-applicable vs
      rebuild-only by worker judgement.
    - ``docs`` -- any ``README.md``, ``CLAUDE.md``, ``changelog/**``, and
      top-level ``*.md``.
    - ``other`` -- anything else.
    """
    is_manifest = Path(path).name in _MANIFEST_BASENAMES
    project = _project_for_path(path)

    # A README is documentation wherever it lives -- without this, a README
    # under a service prefix (e.g. ``libs/bootstrap/README.md``) would inherit
    # that prefix's reveal class and trigger a pointless restart.
    if Path(path).name == "README.md":
        return PathClass(CLASS_DOCS, project, is_manifest)
    # Provisioning files are matched before the generic ``scripts/`` and
    # catch-all rules below: a toolchain script lives under ``scripts/`` (would
    # otherwise read as ``shared_runtime``) and ``.mngr/settings.toml`` would
    # otherwise fall through to ``other`` -- either way the reveal would miss its
    # build/create-time impact.
    if _is_provisioner(path):
        return PathClass(CLASS_PROVISIONER, project, is_manifest)
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


# --- Skill bootstrap -------------------------------------------------------


def read_tree(root: Path) -> dict[str, bytes]:
    """Map every regular file under ``root`` to its bytes, keyed by relative path.

    Keys are POSIX-relative paths so the mapping is comparable across two
    checkouts of the same tree. Directories and symlinks are skipped -- only file
    contents bear on whether two copies of the skill are byte-identical.
    """
    tree: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            tree[path.relative_to(root).as_posix()] = path.read_bytes()
    return tree


def trees_differ(left: Path, right: Path) -> bool:
    """Whether two directory trees differ in their file set or any file's content.

    A missing tree (either side absent) counts as an empty tree, so comparing an
    existing tree against a missing one reports ``True``.
    """
    left_tree = read_tree(left) if left.is_dir() else {}
    right_tree = read_tree(right) if right.is_dir() else {}
    return left_tree != right_tree


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


def _repo_root(args: argparse.Namespace) -> Path:
    """The ``--repo-root`` value, whether given before or after the subcommand.

    The attribute is absent (not defaulted) when the flag was never passed --
    see the ``SUPPRESS`` note in ``main`` -- so the cwd fallback lives here.
    """
    return getattr(args, "repo_root", Path.cwd())


def _cmd_resolve_target(args: argparse.Namespace) -> int:
    repo_root = _repo_root(args)
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
    repo_root = _repo_root(args)
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


def _cmd_changelog_entries(args: argparse.Namespace) -> int:
    repo_root = _repo_root(args)
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


def _cmd_bootstrap_skill(args: argparse.Namespace) -> int:
    repo_root = _repo_root(args).resolve()
    dest = Path(args.dest)
    dest_root = (dest if dest.is_absolute() else repo_root / dest).resolve()

    # If the target ref predates the skill, there is nothing to bootstrap from --
    # report no staged copy so the caller stays on the local flow.
    exists = subprocess.run(
        ["git", "cat-file", "-e", f"{args.ref}:{SKILL_DIR_REL}"],
        cwd=repo_root,
        capture_output=True,
    )
    if exists.returncode != 0:
        print(json.dumps({"skill_dir": None, "differs": False, "ref": args.ref}))
        return 0

    # Extract the ref's own copy of the skill via ``git archive`` (reads the
    # already-fetched object, no network, no working-tree mutation) into a clean
    # staging dir. The archive lays the tree down under ``SKILL_DIR_REL``.
    if dest_root.exists():
        shutil.rmtree(dest_root)
    dest_root.mkdir(parents=True)
    archive = subprocess.run(
        ["git", "archive", args.ref, SKILL_DIR_REL],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(dest_root, filter="data")

    staged_skill = dest_root / SKILL_DIR_REL
    differs = trees_differ(staged_skill, repo_root / SKILL_DIR_REL)
    print(
        json.dumps(
            {"skill_dir": str(staged_skill), "differs": differs, "ref": args.ref}
        )
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    # ``--repo-root`` lives on a shared parent parser so it is accepted both
    # before and after the subcommand (an option defined only on the top-level
    # parser would reject ``update_self.py <subcommand> --repo-root X``).
    # The default must be ``SUPPRESS``, not a value: on Python < 3.13 a
    # subparser re-applies its defaults over the namespace the top-level parser
    # already filled in (bpo-9351), so a concrete default here would clobber a
    # ``--repo-root`` given before the subcommand. With ``SUPPRESS`` the
    # attribute is only set when the flag is actually passed; ``_repo_root``
    # falls back to cwd.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--repo-root",
        type=Path,
        default=argparse.SUPPRESS,
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

    changelog_parser = sub.add_parser(
        "changelog-entries",
        help="List changelog/ entries newly added between two refs.",
        parents=[common],
    )
    changelog_parser.add_argument("--base", required=True, help="Base ref.")
    changelog_parser.add_argument("--target", required=True, help="Target ref.")
    changelog_parser.set_defaults(func=_cmd_changelog_entries)

    bootstrap_parser = sub.add_parser(
        "bootstrap-skill",
        help="Extract the target ref's own update-self skill into a staging dir "
        "and report whether it differs from the local copy.",
        parents=[common],
    )
    bootstrap_parser.add_argument(
        "--ref", required=True, help="The resolved target ref to extract the skill from."
    )
    bootstrap_parser.add_argument(
        "--dest",
        default="runtime/update-self/skill-at-target",
        help="Staging dir the skill is extracted into (default: "
        "runtime/update-self/skill-at-target).",
    )
    bootstrap_parser.set_defaults(func=_cmd_bootstrap_skill)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
