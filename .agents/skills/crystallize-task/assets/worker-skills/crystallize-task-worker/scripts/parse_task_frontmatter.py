#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""Parse a worker task file's YAML frontmatter and emit the required fields.

Pins the schema so workers can't silently consume a task file whose
`lead_agent` / `lead_report_dir` / `transcript_path` was missing,
misspelled, or the wrong type.

The positional argument is a path that may contain a shell-style glob
(e.g. ``runtime/crystallize/*/task.md``). The helper resolves the
glob itself and fails loudly if zero or multiple files match -- so a
worker whose runtime layout drifts (missing task file, or two copies
landing in the same tree) cannot silently parse the wrong thing.
Quote the pattern in the shell (``'runtime/crystallize/*/task.md'``)
so the literal glob reaches this script.

On success (exit 0) prints three shell-evalable `KEY=value` lines to
stdout (values quoted via ``shlex.quote`` so whitespace and shell
metacharacters survive):

    LEAD_AGENT=crystallize-test
    LEAD_REPORT_DIR=runtime/update/foo/reports/
    TRANSCRIPT_PATH=runtime/update/foo/turn.jsonl

On any failure -- no glob match, multiple glob matches, file missing,
no/broken frontmatter, any required field missing, wrong type, or
empty string -- prints a human-readable error to stderr and exits 1.
Unknown extra keys in the frontmatter are ignored (room for future
additions without a breaking change).
"""

from __future__ import annotations

import argparse
import glob
import shlex
import sys
from pathlib import Path
from typing import Any

import yaml


_REQUIRED_FIELDS = ("lead_agent", "lead_report_dir", "transcript_path")


def resolve(pattern: str) -> Path:
    """Return the single path matching ``pattern`` (treated as a glob).

    Raises ``ValueError`` if zero or more than one paths match. A
    literal (non-glob) path still goes through this function -- if the
    path exists, glob returns a single-element list; if not, glob
    returns an empty list and we report it as a missing match.
    """
    matches = sorted(glob.glob(pattern))
    if not matches:
        raise ValueError(f"no task file matches pattern: {pattern}")
    if len(matches) > 1:
        joined = ", ".join(matches)
        raise ValueError(
            f"pattern matches {len(matches)} files (want exactly 1): {joined}"
        )
    return Path(matches[0])


def _split_frontmatter(text: str) -> dict[str, Any]:
    """Parse leading ``---`` YAML frontmatter; return the mapping.

    Raises ``ValueError`` if the frontmatter is missing, unterminated,
    not valid YAML, or not a mapping.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("task file must start with `---` frontmatter delimiter")
    try:
        end_idx = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError("task file frontmatter is not terminated with `---`") from exc
    fm_text = "\n".join(lines[1:end_idx])
    try:
        parsed = yaml.safe_load(fm_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"task file frontmatter is not valid YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("task file frontmatter must be a YAML mapping")
    return parsed


def parse(task_file: Path) -> dict[str, str]:
    """Return the three required fields as a typed dict.

    Raises ``ValueError`` with a precise message on any schema violation.
    """
    if not task_file.is_file():
        raise ValueError(f"task file not found: {task_file}")
    frontmatter = _split_frontmatter(task_file.read_text(encoding="utf-8"))
    result: dict[str, str] = {}
    for field in _REQUIRED_FIELDS:
        if field not in frontmatter:
            raise ValueError(f"frontmatter is missing required field `{field}`")
        value = frontmatter[field]
        if not isinstance(value, str):
            raise ValueError(
                f"frontmatter.{field} must be a string, got {type(value).__name__}"
            )
        if not value:
            raise ValueError(f"frontmatter.{field} must not be empty")
        result[field] = value
    return result


def _render(fields: dict[str, str]) -> str:
    lines = [f"{field.upper()}={shlex.quote(fields[field])}" for field in _REQUIRED_FIELDS]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "pattern",
        help=(
            "Path or shell-style glob pattern resolving to exactly one "
            "worker task file (markdown with YAML frontmatter). Quote "
            "the pattern in the shell so the literal glob reaches this "
            "script."
        ),
    )
    args = parser.parse_args()

    try:
        task_file = resolve(args.pattern)
        fields = parse(task_file)
    except ValueError as exc:
        print(f"invalid task frontmatter: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(_render(fields))
    return 0


if __name__ == "__main__":
    sys.exit(main())
