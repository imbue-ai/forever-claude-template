"""Tests for ``validate_skill.py``.

Run via: ``uv run pytest
.agents/shared/scripts/validate_skill_test.py``
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent / "validate_skill.py"
_spec = importlib.util.spec_from_file_location("validate_skill", _SCRIPT)
assert _spec is not None and _spec.loader is not None
validate_skill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validate_skill)


def _write_skill(
    base: Path,
    name: str,
    description: str = "Valid description",
    body_lines: int = 5,
    metadata_crystallized: bool = False,
    include_run_py: bool | None = None,
    run_py_has_pep723: bool = True,
    run_py_has_workspace_marker: bool = False,
    frontmatter_override: str | None = None,
) -> Path:
    """Build a skill directory on disk; return the skill path."""
    skill = base / name
    skill.mkdir(parents=True)
    meta = "\nmetadata:\n  crystallized: true" if metadata_crystallized else ""
    fm = frontmatter_override if frontmatter_override is not None else (
        f"---\nname: {name}\ndescription: {description}{meta}\n---\n"
    )
    body = "\n".join(f"line {i}" for i in range(body_lines))
    (skill / "SKILL.md").write_text(fm + body)
    if include_run_py is None:
        include_run_py = metadata_crystallized
    if include_run_py:
        scripts = skill / "scripts"
        scripts.mkdir()
        header = ""
        if run_py_has_pep723:
            header += "# /// script\n# requires-python = \">=3.11\"\n# ///\n"
        if run_py_has_workspace_marker:
            header += "# workspace-script: runs in the monorepo uv workspace venv\n"
        (scripts / "run.py").write_text(f"#!/usr/bin/env python3\n{header}print('hi')\n")
    return skill


def test_valid_skill(tmp_path: Path) -> None:
    skill = _write_skill(tmp_path, "my-skill")
    assert validate_skill.validate(skill) is None


def test_name_mismatch(tmp_path: Path) -> None:
    skill = tmp_path / "dirname"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: wrong\ndescription: x\n---\nbody\n")
    error = validate_skill.validate(skill)
    assert error is not None
    assert "does not match parent directory" in error


def test_missing_description(tmp_path: Path) -> None:
    skill = tmp_path / "s"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: s\n---\nbody\n")
    error = validate_skill.validate(skill)
    assert error is not None
    assert "description" in error


def test_description_too_long(tmp_path: Path) -> None:
    skill = _write_skill(tmp_path, "s", description="x" * 2000)
    error = validate_skill.validate(skill)
    assert error is not None
    assert "length" in error


def test_body_too_long(tmp_path: Path) -> None:
    skill = _write_skill(tmp_path, "s", body_lines=600)
    error = validate_skill.validate(skill)
    assert error is not None
    assert "500" in error


def test_crystallized_without_run_py_is_ok(tmp_path: Path) -> None:
    """Crystallized skills do not require run.py -- pure-prose skills are valid."""
    skill = _write_skill(
        tmp_path, "s", metadata_crystallized=True, include_run_py=False
    )
    assert validate_skill.validate(skill) is None


def test_run_py_without_header_or_marker_is_invalid(tmp_path: Path) -> None:
    """A run.py with neither a PEP 723 header nor a workspace marker is invalid."""
    skill = _write_skill(
        tmp_path, "s", metadata_crystallized=True, run_py_has_pep723=False
    )
    error = validate_skill.validate(skill)
    assert error is not None
    assert "PEP 723" in error and "workspace-script" in error


def test_non_crystallized_run_py_also_needs_header_or_marker(tmp_path: Path) -> None:
    """The run.py header/marker requirement holds even when not crystallized."""
    skill = _write_skill(
        tmp_path, "s", include_run_py=True, run_py_has_pep723=False
    )
    error = validate_skill.validate(skill)
    assert error is not None
    assert "PEP 723" in error and "workspace-script" in error


def test_run_py_with_workspace_marker_is_ok(tmp_path: Path) -> None:
    """A header-less run.py is valid when it carries the workspace-script marker
    (the form used for steps that import an unpublished workspace lib)."""
    skill = _write_skill(
        tmp_path,
        "s",
        metadata_crystallized=True,
        run_py_has_pep723=False,
        run_py_has_workspace_marker=True,
    )
    assert validate_skill.validate(skill) is None


def test_run_py_with_both_header_and_marker_is_invalid(tmp_path: Path) -> None:
    """A run.py carrying both a PEP 723 header and the workspace marker is
    invalid -- the header would force isolation and defeat the marker."""
    skill = _write_skill(
        tmp_path,
        "s",
        metadata_crystallized=True,
        run_py_has_pep723=True,
        run_py_has_workspace_marker=True,
    )
    error = validate_skill.validate(skill)
    assert error is not None
    assert "both" in error and "workspace-script" in error


def test_crystallized_with_pep723_ok(tmp_path: Path) -> None:
    skill = _write_skill(tmp_path, "s", metadata_crystallized=True)
    assert validate_skill.validate(skill) is None


def test_missing_frontmatter(tmp_path: Path) -> None:
    skill = tmp_path / "s"
    skill.mkdir()
    (skill / "SKILL.md").write_text("no frontmatter here\n")
    error = validate_skill.validate(skill)
    assert error is not None
    assert "frontmatter" in error


def test_malformed_frontmatter(tmp_path: Path) -> None:
    skill = tmp_path / "s"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: s\ndescription: x\n")  # no closing ---
    error = validate_skill.validate(skill)
    assert error is not None


def test_missing_skill_md(tmp_path: Path) -> None:
    skill = tmp_path / "s"
    skill.mkdir()
    error = validate_skill.validate(skill)
    assert error is not None
    assert "SKILL.md" in error


def test_missing_directory(tmp_path: Path) -> None:
    error = validate_skill.validate(tmp_path / "does-not-exist")
    assert error is not None


@pytest.mark.parametrize(
    "bad_name",
    [
        "Bad-Name",  # uppercase
        "bad_name",  # underscore
        "-leading",  # leading hyphen
        "trailing-",  # trailing hyphen
        "double--hyphen",  # consecutive hyphens
    ],
)
def test_invalid_name_format(tmp_path: Path, bad_name: str) -> None:
    """frontmatter.name must match the kebab-case rules even if dir matches."""
    skill = _write_skill(tmp_path, bad_name)
    error = validate_skill.validate(skill)
    assert error is not None
    assert "lowercase letters/digits" in error


def test_name_too_long(tmp_path: Path) -> None:
    long_name = "a" * 65
    skill = _write_skill(tmp_path, long_name)
    error = validate_skill.validate(skill)
    assert error is not None
    assert "length" in error
