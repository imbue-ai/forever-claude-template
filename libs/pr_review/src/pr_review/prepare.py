"""Opt-in "prepare a repo for rich types": install dependencies and set up a
TypeScript language server for a cached repo tree, driven by a headless agent.

tree-sitter (``jsintel``) works everywhere with zero setup but cannot infer
types or resolve third-party members (e.g. ``session.fromPartition`` where
``session`` comes from ``require('electron')``). For the few repos a user
actually reviews, this module runs a one-shot ``claude -p`` agent *inside* the
cached source tree to install dependencies (npm / pnpm / ...), ensure
``typescript`` is present, and add config so a TypeScript language server can
resolve types. The agent is used because the install shape is too irregular to
hardcode (npm vs pnpm, no root manifest, multiple package dirs, monorepos).

State lives in a ``.pr-review-prep/`` sidecar next to the source root (not inside
it, so it never shows up in file listings). ``tsintel`` consumes ``roots`` /
``typescript_dir`` from ``status.json`` once the state is ``ready``. Nothing here
runs automatically -- it is triggered only by an explicit user action, and it
installs dependencies (running arbitrary ``postinstall`` scripts), so it is
strictly opt-in.

The ``claude -p`` invocation is a compact, dependency-free adaptation of the
copyable helper documented by the ``use-ai-integration`` skill
(``scripts/claude_p.py``): it keeps the load-bearing bits -- unsetting
``MAIN_CLAUDE_SESSION_ID`` so the child is not mistaken for the managed main
session, ``--permission-mode bypassPermissions`` for a headless run, and strict
parsing of the JSON result -- but runs in the tree's working directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

from pr_review.agent_stream import AgentError, AgentRun, run_streaming_agent
from pr_review.github import DATA_DIR, RepoTree, _safe_slug

PREP_DIRNAME = ".pr-review-prep"

# A completed prep (the isolated typescript@5 plus each project's node_modules)
# is keyed by a fingerprint of the repo's *dependency* files, not by commit SHA.
# Two PRs -- or two pushes to one PR -- whose package.json/lockfiles are identical
# reuse the same installed prep instead of re-running the multi-minute agent. The
# store lives outside any single checkout so it survives when disposable per-SHA
# trees are evicted.
PREP_STORE = DATA_DIR / "prep"

# The files whose contents define a dependency set. A commit that touches none of
# these produces the same fingerprint, so its prep is reusable.
_DEP_FILENAMES = (
    "package.json",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "yarn.lock",
)

# Directories never descended into when fingerprinting or capturing artifacts.
_ARTIFACT_DIRNAMES = frozenset({"node_modules", PREP_DIRNAME})

# Setting up an install across an unfamiliar repo is real agentic reasoning, so
# default to a stronger model than the haiku default; the run is explicit and
# rare. The user can pick a different one per run from the dialog.
DEFAULT_MODEL = "claude-sonnet-4-6"
_ALLOWED_MODELS = ("claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5")
_AGENT_TIMEOUT_S = 1800


def normalize_model(model: str | None) -> str:
    """The requested model if it is one we allow, else the default."""
    return model if model in _ALLOWED_MODELS else DEFAULT_MODEL

_AGENT_PROMPT = """\
You are preparing a checked-out copy of a Git repository so that a TypeScript \
language server (tsserver) can resolve types for its JavaScript/TypeScript \
files, including types that come from third-party dependencies.

Your current working directory is the root of the repository checkout.

Do the following:
1. Find the JavaScript/TypeScript project(s): locate every package.json (ignore \
any under node_modules). Determine the package manager from the lockfile present \
(package-lock.json -> npm, pnpm-lock.yaml -> pnpm, yarn.lock -> yarn); default to \
npm if there is no lockfile.
2. Install dependencies in each relevant project directory with that package \
manager (e.g. `npm install`, `pnpm install --no-frozen-lockfile`). This can take \
several minutes; let it finish.
3. Install a TypeScript 5.x for the language server in an ISOLATED directory so \
it does not clobber the repo's own typescript and so we get the classic language \
service API (TypeScript 7.x does NOT expose it): create a `.pr-review-prep/` \
directory at the repo root and run `npm install --prefix .pr-review-prep \
typescript@5` there. Do NOT rely on `npm install typescript` without a version \
(that now installs 7.x, which is unusable here).
4. If it helps type/module resolution for plain JavaScript files, add a \
permissive `jsconfig.json` or `tsconfig.json` at a project root with `allowJs` \
enabled and `checkJs` disabled. Do NOT overwrite an existing config file.
5. Verify it works: confirm `node -e "require.resolve('typescript')"` run with \
cwd `.pr-review-prep` succeeds, and that `require('typescript').createLanguageService` \
is a function (i.e. it is a 5.x, not 7.x).
6. Write a JSON file at `.pr-review-prep/agent_result.json` with EXACTLY these keys:
   - "package_manager": the manager you used for the repo's deps (e.g. "npm" or "pnpm")
   - "roots": array of directory paths, relative to the repo root, where the \
reviewed files live (the project dirs you installed dependencies into)
   - "typescript_dir": ".pr-review-prep" (where the language-server typescript@5 resolves)
   - "notes": a short summary of what you did and anything notable

Keep going until dependencies are installed and typescript resolves. Then give a \
concise final summary."""

_AGENT_APPEND_SYSTEM = (
    "You are preparing a repository checkout for type analysis. Only create or "
    "modify files inside the current working directory (the checkout). Do not "
    "touch anything outside it, and do NOT modify the host system: no `apt`/`brew`/"
    "`curl | sh`, no global or system-wide installs, no changing the installed "
    "Node/npm/pnpm versions. Use the package managers already on PATH; if a "
    "lockfile's engine constraints reject the available version, install with the "
    "engine check relaxed (e.g. `npm install --engine-strict=false`) rather than "
    "installing a different runtime. The only shell commands you should run are "
    "for in-tree dependency installation and verification -- no destructive "
    "operations."
)


class PrepareError(RuntimeError):
    """Raised when the prepare agent fails to run or its output is unusable."""


# Launcher seam: production spawns a background thread that runs the real agent;
# tests inject a fake that writes a terminal status synchronously.
Launcher = Callable[[RepoTree], None]


def _prep_dir(tree: RepoTree) -> Path:
    # Lives at the source-tree root (the prepare agent runs with this as its cwd,
    # and may only write inside it). Excluded from file listing / search like
    # node_modules, so it never shows up in the UI.
    return tree.root / PREP_DIRNAME


def _status_path(tree: RepoTree) -> Path:
    return _prep_dir(tree) / "status.json"


def _log_path(tree: RepoTree) -> Path:
    return _prep_dir(tree) / "prepare.log"


def _agent_result_path(tree: RepoTree) -> Path:
    return _prep_dir(tree) / "agent_result.json"


def _iter_dep_files(root: Path) -> list[Path]:
    """Every dependency-defining file under ``root`` (skipping installed artifacts)."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune artifact dirs in place so os.walk never descends into them.
        dirnames[:] = [d for d in dirnames if d not in _ARTIFACT_DIRNAMES]
        for name in filenames:
            if name in _DEP_FILENAMES:
                found.append(Path(dirpath) / name)
    return found


def dep_fingerprint(root: Path) -> str | None:
    """A stable hash of the repo's dependency files, or ``None`` if it has none.

    Keyed on each file's repo-relative path and byte contents, so two checkouts
    with identical dependency manifests -- regardless of commit SHA or any change
    to non-dependency source -- hash the same and can share an installed prep.
    Files are hashed on the *pristine* tree (before any install rewrites a
    lockfile), so a fresh checkout matches what a prior run published.
    """
    files = _iter_dep_files(root)
    if not files:
        return None
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda p: p.relative_to(root).as_posix()):
        rel = path.relative_to(root).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:32]


def _store_entry(repo: str, fingerprint: str) -> Path:
    return PREP_STORE / _safe_slug(repo) / fingerprint


def _store_manifest_path(entry: Path) -> Path:
    return entry / "manifest.json"


def _entry_is_ready(entry: Path) -> bool:
    """Whether ``entry`` holds a complete, ready-state published prep."""
    manifest = _store_manifest_path(entry)
    status = entry / "prep" / "status.json"
    if not manifest.exists() or not status.exists():
        return False
    try:
        return json.loads(status.read_text()).get("state") == "ready"
    except (ValueError, OSError):
        return False


def reusable_entry(tree: RepoTree) -> Path | None:
    """The shared-store entry a pristine ``tree`` can reuse, or ``None``.

    Fingerprints the tree and returns the matching ready entry if one exists.
    """
    fingerprint = dep_fingerprint(tree.root)
    if fingerprint is None:
        return None
    entry = _store_entry(tree.repo, fingerprint)
    return entry if _entry_is_ready(entry) else None


def _publish(tree: RepoTree, fingerprint: str, roots: list[str]) -> None:
    """Copy a freshly-prepared tree's artifacts into the shared store.

    Captures the ``.pr-review-prep`` sidecar (with its pinned typescript@5) and
    each project root's ``node_modules`` under a fingerprint-keyed entry, so a
    later checkout with the same dependencies can reuse it without reinstalling.
    Best-effort: a failure here leaves the just-prepared tree fully working.
    """
    entry = _store_entry(tree.repo, fingerprint)
    if _entry_is_ready(entry):
        return  # another checkout already published this dependency set
    prep_src = _prep_dir(tree)
    if not prep_src.is_dir():
        return
    entry.parent.mkdir(parents=True, exist_ok=True)
    staging = entry.parent / f".staging-{fingerprint}-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        shutil.copytree(prep_src, staging / "prep", symlinks=True)
        modules = staging / "modules"
        modules.mkdir()
        captured: list[dict] = []
        for idx, root in enumerate(roots):
            nm = (tree.root / root / "node_modules").resolve()
            if nm.is_dir() and nm.is_relative_to(tree.root.resolve()):
                shutil.copytree(nm, modules / str(idx), symlinks=True)
                captured.append({"root": root, "modules": str(idx)})
        _store_manifest_path(staging).write_text(
            json.dumps({"fingerprint": fingerprint, "roots": roots, "modules": captured}, indent=2)
        )
        shutil.rmtree(entry, ignore_errors=True)
        os.replace(staging, entry)
    except (OSError, shutil.Error):
        shutil.rmtree(staging, ignore_errors=True)


def _link(target: Path, source: Path) -> None:
    """Point ``target`` at ``source`` via a symlink, replacing whatever is there."""
    if target.is_symlink() or target.exists():
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target, ignore_errors=True)
        else:
            target.unlink(missing_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Absolute target: the store and checkout paths are relative to the app's cwd,
    # and a relative symlink would resolve against the link's own directory.
    try:
        target.symlink_to(source.resolve())
    except FileExistsError:
        # A concurrent auto-enable won the race; the link is already there.
        pass


def _materialize(tree: RepoTree, entry: Path) -> bool:
    """Symlink a store entry's artifacts into ``tree`` so its rich types work.

    Returns True once the prep sidecar and captured node_modules are linked in.
    """
    try:
        manifest = json.loads(_store_manifest_path(entry).read_text())
    except (ValueError, OSError):
        return False
    _link(_prep_dir(tree), entry / "prep")
    for captured in manifest.get("modules") or []:
        root = captured.get("root")
        rel = captured.get("modules")
        if not isinstance(root, str) or not isinstance(rel, str):
            continue
        src = entry / "modules" / rel
        if src.is_dir():
            _link(tree.root / root / "node_modules", src)
    return True


def _ready_entries(repo: str) -> list[Path]:
    """All ready store entries for ``repo``, most recently published first."""
    base = PREP_STORE / _safe_slug(repo)
    if not base.is_dir():
        return []
    entries = [e for e in base.iterdir() if e.is_dir() and _entry_is_ready(e)]
    return sorted(entries, key=lambda e: e.stat().st_mtime, reverse=True)


def _prior_findings(entry: Path) -> dict:
    """The prior run's reported findings (package manager, roots, notes) for a store entry."""
    for name in ("agent_result.json", "status.json"):
        path = entry / "prep" / name
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
        except (ValueError, OSError):
            continue
        if isinstance(data, dict):
            return data
    return {}


def _seed_from_prior(tree: RepoTree, entry: Path) -> dict:
    """Copy a prior prep's artifacts into ``tree`` as writable starting state.

    Unlike ``_materialize`` (symlinks into the shared store for an *exact* match),
    this makes independent *copies* -- the install agent will mutate them to
    reconcile the differing dependencies, so they must not alias the store. Copies
    the already-built typescript@5 sidecar and each prior root's ``node_modules``
    so the package manager updates incrementally instead of installing cold.
    Returns the prior findings for use as agent context. Best-effort throughout.
    """
    findings = _prior_findings(entry)
    try:
        manifest = json.loads(_store_manifest_path(entry).read_text())
    except (ValueError, OSError):
        manifest = {}
    prep_src = entry / "prep"
    prep_dst = _prep_dir(tree)
    if prep_src.is_dir():
        try:
            if prep_dst.is_symlink():
                prep_dst.unlink()
            elif prep_dst.exists():
                shutil.rmtree(prep_dst, ignore_errors=True)
            shutil.copytree(prep_src, prep_dst, symlinks=True)
            # The copy carries the prior run's terminal status/results; drop them so
            # a later read never mistakes stale output for this run's.
            (prep_dst / "agent_result.json").unlink(missing_ok=True)
            (prep_dst / "status.json").unlink(missing_ok=True)
        except (OSError, shutil.Error):
            pass
    for captured in manifest.get("modules") or []:
        root = captured.get("root")
        rel = captured.get("modules")
        if not isinstance(root, str) or not isinstance(rel, str):
            continue
        src = entry / "modules" / rel
        dst = tree.root / root / "node_modules"
        if src.is_dir() and (tree.root / root).is_dir() and not dst.exists():
            try:
                shutil.copytree(src, dst, symlinks=True)
            except (OSError, shutil.Error):
                pass
    return findings


def _seed_hint(findings: dict) -> str:
    """A prompt preamble describing prior-run state the seeded agent can build on."""
    parts = [
        "This repository was prepared before. That prior preparation's installed "
        "dependencies and its typescript@5 sidecar (.pr-review-prep) have ALREADY "
        "been copied into this checkout. Reconcile them with the current manifests "
        "-- run the package manager's install, which will update incrementally -- "
        "instead of installing from scratch, and reuse the existing .pr-review-prep "
        "rather than reinstalling typescript."
    ]
    pm = findings.get("package_manager")
    if isinstance(pm, str) and pm:
        parts.append(f"Package manager used previously: {pm}.")
    roots = [r for r in (findings.get("roots") or []) if isinstance(r, str)]
    if roots:
        parts.append("Project roots found previously: " + ", ".join(roots) + ".")
    notes = findings.get("notes")
    if isinstance(notes, str) and notes:
        parts.append("Notes from the previous run (repo-specific gotchas):\n" + notes)
    return "\n\n".join(parts)


def _build_prompt(seed_hint: str | None) -> str:
    """The agent prompt, with an optional prior-preparation context section prepended."""
    if not seed_hint:
        return _AGENT_PROMPT
    return f"PRIOR PREPARATION CONTEXT (use it to go faster):\n{seed_hint}\n\n{_AGENT_PROMPT}"


def prepare_status(tree: RepoTree) -> dict:
    """The current prepare state for ``tree`` (``{"state": "absent"}`` if none)."""
    path = _status_path(tree)
    if not path.exists():
        return {"state": "absent"}
    try:
        return json.loads(path.read_text())
    except ValueError:
        return {"state": "absent"}


def is_ready(tree: RepoTree) -> bool:
    return prepare_status(tree).get("state") == "ready"


def auto_enable(tree: RepoTree) -> dict:
    """Silently enable rich types for ``tree`` iff it needs no install agent.

    When the tree has no prep yet and the shared store holds an exact
    dependency-fingerprint match, that reuse is free (symlinks, no agent), so we
    materialize it and report ``ready`` without the user asking. When an install
    would be required (no match, or only a partial one to seed from), this is a
    no-op and rich types stay opt-in behind the explicit Enable action.
    """
    current = prepare_status(tree)
    if current.get("state") != "absent":
        return current
    entry = reusable_entry(tree)
    if entry is None:
        return current
    try:
        if _materialize(tree, entry):
            return prepare_status(tree)
    except OSError:
        pass  # a later call retries; keep the app responsive
    return current


def ready_roots(tree: RepoTree) -> list[str]:
    """Project roots the agent set up, for a ready tree (empty otherwise)."""
    status = prepare_status(tree)
    if status.get("state") != "ready":
        return []
    roots = status.get("roots") or []
    return [r for r in roots if isinstance(r, str)]


def log_tail(tree: RepoTree, lines: int = 50) -> str:
    path = _log_path(tree)
    if not path.exists():
        return ""
    return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])


def _write_status(tree: RepoTree, status: dict) -> None:
    prep = _prep_dir(tree)
    prep.mkdir(parents=True, exist_ok=True)
    _status_path(tree).write_text(json.dumps(status, indent=2))


def start_prepare(
    tree: RepoTree, launcher: Launcher | None = None, force: bool = False, model: str | None = None
) -> dict:
    """Kick off preparation for ``tree`` (idempotent).

    Returns the current status without relaunching when a run is already in
    flight (``installing``) or complete (``ready``), unless ``force`` is set.
    ``model`` selects the agent model (validated against the allow-list).
    """
    chosen = normalize_model(model)
    launcher = launcher or (lambda t: _default_launcher(t, chosen))
    current = prepare_status(tree)
    if not force and current.get("state") in ("installing", "ready"):
        return current
    # Reuse an installed prep from a prior checkout with the same dependencies
    # (a different PR, or an earlier push) instead of re-running the agent.
    if not force:
        entry = reusable_entry(tree)
        if entry is not None and _materialize(tree, entry):
            return prepare_status(tree)
    status = {"state": "installing", "model": chosen, "error": None}
    _write_status(tree, status)
    launcher(tree)
    return status


def clear_prepared(tree: RepoTree) -> dict:
    """Remove this checkout's prepared state to reclaim disk.

    Handles both a freshly-installed tree (real ``node_modules`` / sidecar) and a
    reused one (symlinks into the shared store): real dirs are deleted, symlinks
    are unlinked. The shared store itself is left intact so other checkouts of the
    same dependency set keep reusing it -- this only clears the local checkout.
    """
    root = tree.root
    for dirpath, dirnames, _files in os.walk(root):
        # Never descend into the sidecar (its node_modules belongs to the prep).
        if PREP_DIRNAME in dirnames:
            dirnames.remove(PREP_DIRNAME)
        if "node_modules" in dirnames:
            nm = Path(dirpath) / "node_modules"
            if nm.is_symlink():
                nm.unlink(missing_ok=True)
            elif nm.is_dir():
                shutil.rmtree(nm, ignore_errors=True)
            dirnames.remove("node_modules")  # don't descend into what we just removed
    prep = _prep_dir(tree)
    if prep.is_symlink():
        prep.unlink(missing_ok=True)
    else:
        shutil.rmtree(prep, ignore_errors=True)
    return {"state": "absent"}


def _default_launcher(tree: RepoTree, model: str = DEFAULT_MODEL) -> None:
    threading.Thread(target=_run_prepare, args=(tree, model), daemon=True).start()


def _run_prepare(tree: RepoTree, model: str = DEFAULT_MODEL) -> None:
    # Fingerprint the pristine tree before the agent installs (installers may
    # rewrite lockfiles), so a later fresh checkout with the same deps matches
    # what we publish below.
    fingerprint = dep_fingerprint(tree.root)
    seed_hint = _seed_for_install(tree, fingerprint, model)
    try:
        run = _run_agent(tree, model, seed_hint=seed_hint)
        findings = _read_agent_findings(tree)
        ok, detail = _verify(tree, findings)
        roots = findings.get("roots") or []
        status = {
            "state": "ready" if ok else "failed",
            "model": model,
            "package_manager": findings.get("package_manager"),
            "roots": roots,
            "typescript_dir": findings.get("typescript_dir"),
            "notes": findings.get("notes"),
            "cost_usd": run.cost_usd,
            "error": None if ok else detail,
        }
    except (PrepareError, AgentError, OSError, subprocess.SubprocessError, ValueError) as exc:
        # Any expected failure in this background thread becomes a failed status
        # the UI can show, rather than a silently dead thread.
        status = {"state": "failed", "model": model, "error": str(exc)[:1000]}
    _write_status(tree, status)
    if status["state"] == "ready" and fingerprint is not None:
        # Share the install so sibling checkouts (other PRs, later pushes) reuse it.
        _publish(tree, fingerprint, [r for r in status["roots"] if isinstance(r, str)])


def _seed_for_install(tree: RepoTree, fingerprint: str | None, model: str) -> str | None:
    """Seed a from-scratch install with the repo's nearest previous prep, if any.

    Copies the most recent ready prep for this repo (with a *different*
    fingerprint -- an exact match would have been reused, not reinstalled) into the
    checkout so the agent updates incrementally, and returns a prompt hint carrying
    what that prior run learned. Returns None when there is nothing to seed from.
    """
    priors = [e for e in _ready_entries(tree.repo) if e.name != fingerprint]
    if not priors:
        return None
    findings = _seed_from_prior(tree, priors[0])
    # The seed copy overwrote the 'installing' marker start_prepare wrote; restore
    # it so the UI keeps showing progress rather than the prior run's status.
    _write_status(tree, {"state": "installing", "model": model, "error": None})
    return _seed_hint(findings)


def _run_agent(tree: RepoTree, model: str = DEFAULT_MODEL, seed_hint: str | None = None) -> AgentRun:
    """Run the headless prepare agent in the tree, streaming its activity to the
    log line-by-line so the UI can show live progress while it installs."""
    return run_streaming_agent(
        _build_prompt(seed_hint),
        cwd=tree.root,
        log_path=_log_path(tree),
        model=model,
        append_system_prompt=_AGENT_APPEND_SYSTEM,
        header=f"● Preparing rich types for {tree.repo} — this can take a few minutes.",
        timeout_s=_AGENT_TIMEOUT_S,
    )


def _read_agent_findings(tree: RepoTree) -> dict:
    path = _agent_result_path(tree)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _verify(tree: RepoTree, findings: dict) -> tuple[bool, str]:
    """Independently confirm typescript resolves where the agent said it does."""
    ts_dir = findings.get("typescript_dir")
    if not isinstance(ts_dir, str) or not ts_dir:
        return False, "prepare agent did not report a typescript_dir"
    root = tree.root.resolve()
    abs_dir = (tree.root / ts_dir).resolve()
    if not abs_dir.is_relative_to(root) or not abs_dir.is_dir():
        return False, f"typescript_dir {ts_dir!r} is not a directory inside the tree"
    probe = subprocess.run(
        ["node", "-e", "require.resolve('typescript')"],
        cwd=str(abs_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        return False, f"typescript is not resolvable in {ts_dir!r}"
    return True, ""
