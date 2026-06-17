"""Tests for ``validate_skill.py``.

Run via: ``uv run pytest
.agents/shared/scripts/validate_skill_test.py``
"""

from __future__ import annotations

import importlib.util
import subprocess
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
    frontmatter_override: str | None = None,
) -> Path:
    """Build a skill directory on disk; return the skill path."""
    skill = base / name
    skill.mkdir(parents=True)
    meta = "\nmetadata:\n  crystallized: true" if metadata_crystallized else ""
    fm = (
        frontmatter_override
        if frontmatter_override is not None
        else (f"---\nname: {name}\ndescription: {description}{meta}\n---\n")
    )
    body = "\n".join(f"line {i}" for i in range(body_lines))
    (skill / "SKILL.md").write_text(fm + body)
    if include_run_py is None:
        include_run_py = metadata_crystallized
    if include_run_py:
        scripts = skill / "scripts"
        scripts.mkdir()
        header = (
            '# /// script\n# requires-python = ">=3.11"\n# ///\n'
            if run_py_has_pep723
            else ""
        )
        (scripts / "run.py").write_text(
            f"#!/usr/bin/env python3\n{header}print('hi')\n"
        )
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


def test_run_py_without_pep723_is_invalid(tmp_path: Path) -> None:
    """If run.py is present (crystallized or not), it must have a PEP 723 header."""
    skill = _write_skill(
        tmp_path, "s", metadata_crystallized=True, run_py_has_pep723=False
    )
    error = validate_skill.validate(skill)
    assert error is not None
    assert "PEP 723" in error


def test_non_crystallized_run_py_also_needs_pep723(tmp_path: Path) -> None:
    """run.py must have PEP 723 even when the skill is not crystallized."""
    skill = _write_skill(tmp_path, "s", include_run_py=True, run_py_has_pep723=False)
    error = validate_skill.validate(skill)
    assert error is not None
    assert "PEP 723" in error


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


# --- Runnability check (check_runnable) -------------------------------------

# The real-`uv`-run tests below resolve a PEP 723 environment in a subprocess.
# On a cold uv cache that can far exceed the suite's global 10s per-test
# timeout, so give those tests the same generous budget the validator itself
# allows for a cold resolution (validate_skill._RUN_HELP_TIMEOUT_SECONDS).
_REAL_UV_RUN_TIMEOUT_SECONDS = 180

_PEP723_HEADER = '# /// script\n# requires-python = ">=3.11"\n# ///\n'

# A deps-free script whose argparse `--help` exits 0.
_GOOD_RUN_PY = (
    _PEP723_HEADER
    + "import argparse\n"
    + "def main() -> None:\n"
    + "    argparse.ArgumentParser().parse_args()\n"
    + "if __name__ == '__main__':\n"
    + "    main()\n"
)

# A script that fails to import before argparse ever runs.
_BROKEN_RUN_PY = _PEP723_HEADER + "import this_module_definitely_does_not_exist_xyz\n"


def _skill_with_run_py(base: Path, run_py_body: str) -> Path:
    """A valid skill whose scripts/run.py has the given body."""
    skill = _write_skill(base, "s", metadata_crystallized=True)
    (skill / "scripts" / "run.py").write_text(run_py_body)
    return skill


def test_check_runnable_no_run_py(tmp_path: Path) -> None:
    skill = _write_skill(tmp_path, "s", include_run_py=False)
    assert validate_skill.check_runnable(skill) is None


@pytest.mark.timeout(_REAL_UV_RUN_TIMEOUT_SECONDS)
def test_check_runnable_good_script(tmp_path: Path) -> None:
    """A script that imports cleanly and supports --help passes (real uv run)."""
    skill = _skill_with_run_py(tmp_path, _GOOD_RUN_PY)
    assert validate_skill.check_runnable(skill) is None


@pytest.mark.timeout(_REAL_UV_RUN_TIMEOUT_SECONDS)
def test_check_runnable_broken_import(tmp_path: Path) -> None:
    """A top-level import error is caught by the --help run (real uv run)."""
    skill = _skill_with_run_py(tmp_path, _BROKEN_RUN_PY)
    error = validate_skill.check_runnable(skill)
    assert error is not None
    assert "run.py" in error
    assert "this_module_definitely_does_not_exist_xyz" in error


def test_check_runnable_reports_failure_detail(tmp_path: Path) -> None:
    """A non-zero exit surfaces the script's stderr in the error message."""
    skill = _skill_with_run_py(tmp_path, _GOOD_RUN_PY)

    def _fail(_run_py: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[], returncode=2, stdout="", stderr="boom"
        )

    error = validate_skill.check_runnable(skill, runner=_fail)
    assert error is not None
    assert "exit 2" in error
    assert "boom" in error


def test_check_runnable_timeout(tmp_path: Path) -> None:
    skill = _skill_with_run_py(tmp_path, _GOOD_RUN_PY)

    def _timeout(_run_py: Path) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="uv", timeout=1)

    error = validate_skill.check_runnable(skill, runner=_timeout)
    assert error is not None
    assert "did not respond" in error


def test_check_runnable_uv_missing(tmp_path: Path) -> None:
    skill = _skill_with_run_py(tmp_path, _GOOD_RUN_PY)

    def _no_uv(_run_py: Path) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("uv")

    error = validate_skill.check_runnable(skill, runner=_no_uv)
    assert error is not None
    assert "uv" in error
