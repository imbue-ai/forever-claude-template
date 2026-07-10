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

import json
import os
import shutil
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from pr_review.github import RepoTree

PREP_DIRNAME = ".pr-review-prep"

# Setting up an install across an unfamiliar repo is real agentic reasoning, so
# use a stronger model than the haiku default; the run is explicit and rare.
_AGENT_MODEL = "claude-sonnet-4-6"
_AGENT_TIMEOUT_S = 1800

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
    "touch anything outside it. The only shell commands you should run are for "
    "dependency installation and verification -- no destructive operations."
)


class PrepareError(RuntimeError):
    """Raised when the prepare agent fails to run or its output is unusable."""


class _AgentRun(NamedTuple):
    text: str
    cost_usd: float


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


def start_prepare(tree: RepoTree, launcher: Launcher | None = None, force: bool = False) -> dict:
    """Kick off preparation for ``tree`` (idempotent).

    Returns the current status without relaunching when a run is already in
    flight (``installing``) or complete (``ready``), unless ``force`` is set.
    """
    launcher = launcher or _default_launcher
    current = prepare_status(tree)
    if not force and current.get("state") in ("installing", "ready"):
        return current
    status = {"state": "installing", "error": None}
    _write_status(tree, status)
    launcher(tree)
    return status


def clear_prepared(tree: RepoTree) -> dict:
    """Remove the prepared state and installed ``node_modules`` to reclaim disk.

    Only touches paths inside this disposable cache tree.
    """
    root = tree.root.resolve()
    for node_modules in root.rglob("node_modules"):
        resolved = node_modules.resolve()
        if resolved.is_dir() and str(resolved).startswith(str(root)):
            shutil.rmtree(resolved, ignore_errors=True)
    shutil.rmtree(_prep_dir(tree), ignore_errors=True)
    return {"state": "absent"}


def _default_launcher(tree: RepoTree) -> None:
    threading.Thread(target=_run_prepare, args=(tree,), daemon=True).start()


def _run_prepare(tree: RepoTree) -> None:
    try:
        run = _run_agent(tree)
        findings = _read_agent_findings(tree)
        ok, detail = _verify(tree, findings)
        status = {
            "state": "ready" if ok else "failed",
            "package_manager": findings.get("package_manager"),
            "roots": findings.get("roots") or [],
            "typescript_dir": findings.get("typescript_dir"),
            "notes": findings.get("notes"),
            "cost_usd": run.cost_usd,
            "error": None if ok else detail,
        }
    except (PrepareError, OSError, subprocess.SubprocessError, ValueError) as exc:
        # Any expected failure in this background thread becomes a failed status
        # the UI can show, rather than a silently dead thread.
        status = {"state": "failed", "error": str(exc)[:1000]}
    _write_status(tree, status)


def _run_agent(tree: RepoTree) -> _AgentRun:
    """Run the headless prepare agent in the tree; write its output to the log."""
    argv = [
        "claude", "-p", _AGENT_PROMPT,
        "--output-format", "json",
        "--model", _AGENT_MODEL,
        "--permission-mode", "bypassPermissions",
        "--append-system-prompt", _AGENT_APPEND_SYSTEM,
    ]
    env = dict(os.environ)
    env.pop("MAIN_CLAUDE_SESSION_ID", None)
    proc = subprocess.run(
        argv,
        cwd=str(tree.root),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=_AGENT_TIMEOUT_S,
    )
    text, cost = "", 0.0
    try:
        data = json.loads(proc.stdout)
        text = (data.get("result") or "") if isinstance(data, dict) else ""
        cost = float(data.get("total_cost_usd") or 0.0) if isinstance(data, dict) else 0.0
    except ValueError:
        text = proc.stdout[-4000:]
    log = text
    if proc.stderr.strip():
        log += "\n\n[stderr]\n" + proc.stderr[-2000:]
    _log_path(tree).parent.mkdir(parents=True, exist_ok=True)
    _log_path(tree).write_text(log)
    if proc.returncode != 0:
        raise PrepareError(f"prepare agent exited {proc.returncode}: {proc.stderr.strip()[:300]}")
    return _AgentRun(text=text, cost_usd=cost)


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
    if not str(abs_dir).startswith(str(root)) or not abs_dir.is_dir():
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
