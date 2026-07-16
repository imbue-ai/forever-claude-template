"""Tests for the per-project changelog gate.

The gate maps changed files to the projects that own them and fails when a
touched project is missing its per-PR entry file. The most important behaviors
to lock down are: (1) it refuses to pass vacuously when the only resolvable
diff base is HEAD itself (the sandbox / shallow-clone footgun), and (2) it maps
files to projects and detects missing entries correctly against a real git
repo.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent / "check_changelog_entries.py"
_spec = importlib.util.spec_from_file_location("check_changelog_entries", _SCRIPT)
assert _spec is not None and _spec.loader is not None
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _init_repo(tmp_path: Path) -> Path:
    """Create a minimal monorepo-shaped git repo with a `main` branch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    # Two real projects (pyproject.toml present) plus the dev bucket layout.
    for rel in ("libs/alpha/pyproject.toml", "apps/beta/pyproject.toml"):
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("[project]\nname='x'\n")
    (repo / "README.md").write_text("root\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _add_entry(repo: Path, project_dir: str, branch: str) -> None:
    d = repo / project_dir / "changelog"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{branch.replace('/', '-')}.md").write_text("did a thing\n")


def test_project_for_path_maps_libs_apps_and_dev(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    assert gate.project_for_path("libs/alpha/foo.py", repo) == "alpha"
    assert gate.project_for_path("apps/beta/bar.py", repo) == "beta"
    # A libs/ dir without a pyproject.toml is not a real project -> dev.
    assert gate.project_for_path("libs/nope/x.py", repo) == "dev"
    # Root-level files -> dev.
    assert gate.project_for_path("scripts/thing.sh", repo) == "dev"
    assert gate.project_for_path("README.md", repo) == "dev"


def test_gate_fails_when_touched_project_missing_entry(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "feat/x")
    (repo / "libs/alpha/new.py").write_text("print(1)\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "touch alpha")

    base = gate.resolve_diff_base(repo)
    changed = gate.changed_files_against_base(base, repo)
    touched = gate.projects_requiring_entry(changed, repo)
    assert touched == {"alpha"}
    assert gate.find_missing_entries("feat/x", touched, repo) == [
        "libs/alpha/changelog/feat-x.md"
    ]


def test_gate_passes_when_entry_present(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "feat/y")
    (repo / "apps/beta/new.py").write_text("print(1)\n")
    _add_entry(repo, "apps/beta", "feat/y")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "touch beta with entry")

    base = gate.resolve_diff_base(repo)
    changed = gate.changed_files_against_base(base, repo)
    touched = gate.projects_requiring_entry(changed, repo)
    assert touched == {"beta"}
    assert gate.find_missing_entries("feat/y", touched, repo) == []


def test_resolve_diff_base_refuses_head_collision(tmp_path: Path) -> None:
    """When main == HEAD (e.g. a fresh clone with no distinct base), the gate
    must raise rather than diff against HEAD and pass vacuously."""
    repo = _init_repo(tmp_path)
    # Still on main, so main resolves to HEAD; no other base ref exists.
    with pytest.raises(RuntimeError):
        gate.resolve_diff_base(repo)
