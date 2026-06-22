#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""Validate a skill directory against the agentskills.io spec.

Static structural checks (in order, short-circuit on first failure per check):

- Directory exists.
- `SKILL.md` exists.
- SKILL.md has valid YAML frontmatter (delimited by `---` lines).
- Frontmatter has `name` matching the directory basename.
- Frontmatter has `description`, 1-1024 characters.
- SKILL.md body (after frontmatter) is at most 500 lines.
- If `scripts/run.py` exists, it begins with a PEP 723 `# /// script` header.
  (`run.py` is optional even for crystallized skills -- a skill may be pure
  SKILL.md prose if every step is judgement or uses existing tools.)

Runnability check (only when the static checks pass and `scripts/run.py`
exists):

- `uv run scripts/run.py --help` exits 0. This forces `uv` to resolve the
  script's PEP 723 dependencies and import the module, so a broken import or
  an unresolvable dependency is caught here rather than only at scenario time.
  `--help` is a shallow import check: it exercises the top-level imports and
  argparse wiring, not imports done lazily inside subcommand bodies -- those
  are left to scenario testing.

Exits 0 and prints `ok` when valid; exits 1 with a human-readable error to
stderr otherwise.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

_MAX_BODY_LINES = 500
_MIN_DESC_LEN = 1
_MAX_DESC_LEN = 1024
_MIN_NAME_LEN = 1
_MAX_NAME_LEN = 64
_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# Generous enough to cover a cold `uv` dependency resolution (first-time
# download + install); warm-cache runs are near-instant.
_RUN_HELP_TIMEOUT_SECONDS = 180

# A callable that invokes `run.py --help` and reports the completed process.
# Injectable so tests can exercise the result-handling without a real `uv` run.
RunHelpRunner = Callable[[Path], "subprocess.CompletedProcess[str]"]


def _split_frontmatter(text: str) -> tuple[dict[str, Any], list[str]]:
    """Parse leading ``---`` YAML frontmatter; return (fm_dict, body_lines).

    Raises ``ValueError`` if the frontmatter is missing, malformed, or not a
    mapping.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("SKILL.md must start with `---` frontmatter delimiter")
    try:
        end_idx = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError("SKILL.md frontmatter is not terminated with `---`") from exc
    fm_text = "\n".join(lines[1:end_idx])
    try:
        parsed = yaml.safe_load(fm_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"SKILL.md frontmatter is not valid YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("SKILL.md frontmatter must be a YAML mapping")
    body_lines = lines[end_idx + 1 :]
    return parsed, body_lines


def _validate_run_py(skill_dir: Path) -> str | None:
    """If scripts/run.py exists, require a PEP 723 header. Absent run.py is OK."""
    run_py = skill_dir / "scripts" / "run.py"
    if not run_py.is_file():
        return None
    first_few = run_py.read_text(encoding="utf-8").splitlines()[:5]
    if not any(line.strip().startswith("# /// script") for line in first_few):
        return f"{run_py} is missing a PEP 723 `# /// script` header"
    return None


def _run_help_via_uv(run_py: Path) -> subprocess.CompletedProcess[str]:
    """Invoke ``uv run <run_py> --help`` so uv resolves the script's deps."""
    return subprocess.run(
        ["uv", "run", str(run_py), "--help"],
        capture_output=True,
        text=True,
        timeout=_RUN_HELP_TIMEOUT_SECONDS,
        check=False,
    )


def check_runnable(
    skill_dir: Path, runner: RunHelpRunner = _run_help_via_uv
) -> str | None:
    """Confirm ``scripts/run.py`` runs far enough to import and build its CLI.

    Runs ``uv run scripts/run.py --help`` via ``runner`` (injectable for
    testing). Returns an error message on failure, otherwise ``None``. A skill
    with no ``run.py`` is trivially runnable, so this returns ``None``.
    """
    run_py = skill_dir / "scripts" / "run.py"
    if not run_py.is_file():
        return None
    try:
        result = runner(run_py)
    except subprocess.TimeoutExpired:
        return (
            f"{run_py} did not respond to `--help` within "
            f"{_RUN_HELP_TIMEOUT_SECONDS}s (dependency resolution may be stuck "
            "or the script blocks before argparse)"
        )
    except FileNotFoundError:
        return "`uv` was not found on PATH; cannot run the run.py runnability check"
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        return (
            f"{run_py} failed `uv run ... --help` (exit {result.returncode}); "
            f"its imports or PEP 723 dependencies may be broken:\n{detail}"
        )
    return None


def validate(skill_dir: Path) -> str | None:
    """Return an error message if the skill is invalid; otherwise ``None``."""
    if not skill_dir.is_dir():
        return f"skill directory not found: {skill_dir}"
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return f"SKILL.md not found at {skill_md}"

    try:
        frontmatter, body_lines = _split_frontmatter(
            skill_md.read_text(encoding="utf-8")
        )
    except ValueError as exc:
        return str(exc)

    name = frontmatter.get("name")
    if not isinstance(name, str) or not name:
        return "frontmatter.name is missing or empty"
    if not (_MIN_NAME_LEN <= len(name) <= _MAX_NAME_LEN):
        return (
            f"frontmatter.name length must be {_MIN_NAME_LEN}-{_MAX_NAME_LEN} "
            f"characters, got {len(name)}"
        )
    if not _NAME_PATTERN.fullmatch(name):
        return (
            f"frontmatter.name ({name!r}) must match "
            f"^[a-z0-9]+(?:-[a-z0-9]+)*$ -- lowercase letters/digits "
            "separated by single hyphens, no leading/trailing or consecutive hyphens"
        )
    if name != skill_dir.name:
        return (
            f"frontmatter.name ({name!r}) does not match parent directory "
            f"({skill_dir.name!r})"
        )

    description = frontmatter.get("description")
    if not isinstance(description, str):
        return "frontmatter.description is missing or not a string"
    if not (_MIN_DESC_LEN <= len(description) <= _MAX_DESC_LEN):
        return (
            f"frontmatter.description length must be "
            f"{_MIN_DESC_LEN}-{_MAX_DESC_LEN}, got {len(description)}"
        )

    if len(body_lines) > _MAX_BODY_LINES:
        return (
            f"SKILL.md body is {len(body_lines)} lines; spec recommends "
            f"<= {_MAX_BODY_LINES} (use references/ for overflow)"
        )

    err = _validate_run_py(skill_dir)
    if err is not None:
        return err

    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "skill_dir",
        type=Path,
        help="Path to the skill directory to validate (e.g. .agents/skills/my-skill)",
    )
    args = parser.parse_args()

    error = validate(args.skill_dir)
    if error is None:
        error = check_runnable(args.skill_dir)
    if error is None:
        print("ok")
        return 0
    print(f"invalid skill: {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
