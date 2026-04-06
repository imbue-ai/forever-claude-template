import ast
import re
import subprocess
from pathlib import Path

import pytest
import tomlkit
from inline_snapshot import snapshot

_REPO_ROOT = Path(__file__).parent

# Directories excluded from scanning (vendored code)
_VENDORED_DIR = _REPO_ROOT / "vendor"

_SELF_EXCLUSION: tuple[str, ...] = ("test_meta_ratchets.py",)

pytestmark = pytest.mark.xdist_group(name="meta_ratchets")


def _get_all_project_dirs() -> list[Path]:
    """Return all project directories (libs/*) excluding vendored code."""
    project_dirs: list[Path] = []
    libs_dir = _REPO_ROOT / "libs"
    if not libs_dir.is_dir():
        return project_dirs
    for child in sorted(libs_dir.iterdir()):
        if child.is_dir() and (child / "pyproject.toml").exists():
            project_dirs.append(child)
    return project_dirs


def _find_test_ratchets_file(project_dir: Path) -> Path | None:
    """Find a test_*_ratchets.py file within a project directory."""
    matches = [p for p in project_dir.rglob("test_*_ratchets.py")]
    if len(matches) == 1:
        return matches[0]
    elif len(matches) == 0:
        return None
    else:
        raise AssertionError(
            f"Found multiple test_*_ratchets.py files in {project_dir.name}: "
            + ", ".join(str(m.relative_to(project_dir)) for m in matches)
        )


def _extract_test_function_names(file_path: Path) -> frozenset[str]:
    """Extract all test function names (starting with 'test_') from a Python file using AST."""
    tree = ast.parse(file_path.read_text())
    return frozenset(
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    )


# --- Meta: ensure every project has ratchets ---


def test_every_project_has_test_ratchets_file() -> None:
    """Ensure each project (except excluded ones) has a test_*_ratchets.py file."""
    missing: list[str] = []
    for project_dir in _get_all_project_dirs():
        if _find_test_ratchets_file(project_dir) is None:
            missing.append(project_dir.name)
    assert len(missing) == 0, (
        "The following projects are missing a test_*_ratchets.py file:\n"
        + "\n".join(f"  - {m}" for m in missing)
    )


def test_all_test_ratchets_files_have_same_tests() -> None:
    """Ensure all test_*_ratchets.py files define precisely the same set of test functions."""
    test_names_by_project: dict[str, frozenset[str]] = {}
    for project_dir in _get_all_project_dirs():
        ratchet_file = _find_test_ratchets_file(project_dir)
        if ratchet_file is None:
            continue
        test_names_by_project[project_dir.name] = _extract_test_function_names(
            ratchet_file
        )

    if not test_names_by_project:
        raise AssertionError("No test_*_ratchets.py files found")

    project_names = sorted(test_names_by_project.keys())
    reference_project = project_names[0]
    reference_tests = test_names_by_project[reference_project]

    mismatches: list[str] = []
    for project_name in project_names[1:]:
        project_tests = test_names_by_project[project_name]
        missing_tests = reference_tests - project_tests
        extra_tests = project_tests - reference_tests
        if missing_tests or extra_tests:
            parts = [f"  {project_name} (vs {reference_project}):"]
            if missing_tests:
                parts.append(f"    missing: {sorted(missing_tests)}")
            if extra_tests:
                parts.append(f"    extra:   {sorted(extra_tests)}")
            mismatches.append("\n".join(parts))

    assert len(mismatches) == 0, (
        "test_*_ratchets.py files have different test functions:\n"
        + "\n".join(mismatches)
    )


# --- Repo-wide ratchets ---


def _find_bash_scripts_without_strict_mode() -> list[str]:
    """Find bash scripts missing 'set -euo pipefail', excluding vendored code."""
    violations: list[str] = []
    vendored_prefix = str(_VENDORED_DIR)
    for script in _REPO_ROOT.rglob("*.sh"):
        if str(script).startswith(vendored_prefix):
            continue
        content = script.read_text(errors="replace")
        if re.search(r"^#!/.*bash", content) and "set -euo pipefail" not in content:
            violations.append(str(script.relative_to(_REPO_ROOT)))
    return sorted(violations)


def test_prevent_bash_without_strict_mode() -> None:
    """Ensure all bash scripts use 'set -euo pipefail' for strict error handling."""
    violations = _find_bash_scripts_without_strict_mode()
    assert len(violations) <= snapshot(0), (
        "Bash scripts missing 'set -euo pipefail':\n"
        + "\n".join(f"  - {v}" for v in violations)
    )


def test_every_project_has_pypi_readme() -> None:
    """Ensure each project's pyproject.toml has a readme field pointing to an existing file."""
    missing_field: list[str] = []
    missing_file: list[str] = []

    for project_dir in _get_all_project_dirs():
        pyproject_path = project_dir / "pyproject.toml"
        pyproject = tomlkit.parse(pyproject_path.read_text())
        project_section = pyproject.get("project", {})

        readme_value = project_section.get("readme")
        if not isinstance(readme_value, str):
            missing_field.append(project_dir.name)
            continue

        if not (project_dir / readme_value).exists():
            missing_file.append(f"{project_dir.name} (references {readme_value})")

    errors: list[str] = []
    if missing_field:
        errors.append("Missing readme field in [project]: " + ", ".join(missing_field))
    if missing_file:
        errors.append("readme file does not exist: " + ", ".join(missing_file))

    assert len(errors) == 0, "Projects with PyPI readme issues:\n" + "\n".join(
        f"  - {e}" for e in errors
    )


def _find_tracked_gitignored_files() -> list[str]:
    """Return tracked files that match .gitignore patterns."""
    tracked = subprocess.run(
        ["git", "ls-files"],
        capture_output=True,
        text=True,
        check=True,
        cwd=_REPO_ROOT,
    )
    ignored = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin"],
        input=tracked.stdout,
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
    )
    return [line for line in ignored.stdout.splitlines() if line.strip()]


def test_no_gitignored_files_are_tracked() -> None:
    """Ensure no tracked files match .gitignore patterns."""
    offending = _find_tracked_gitignored_files()
    assert len(offending) == 0, (
        "The following tracked files match .gitignore patterns (remove with `git rm --cached`):\n"
        + "\n".join(f"  - {f}" for f in offending)
    )
