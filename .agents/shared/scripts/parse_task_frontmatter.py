#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""Parse a worker task file's YAML frontmatter and emit its string fields.

Pins the required schema so workers can't silently consume a task file
whose `lead_agent` / `finish_report_path` was missing, misspelled, or the
wrong type. Beyond those two, any additional top-level string fields
the lead sets are passed through to the worker -- so leads can attach
flow-specific context (a ticket id, a feature flag, a list of staged
inputs) without each new key requiring a parser change.

The positional argument is a path that may contain a shell-style glob
(e.g. ``runtime/harden/*/task.md``). The helper resolves the
glob itself and fails loudly if zero or multiple files match -- so a
worker whose runtime layout drifts (missing task file, or two copies
landing in the same tree) cannot silently parse the wrong thing.
Quote the pattern in the shell (``'runtime/harden/*/task.md'``)
so the literal glob reaches this script.

On success (exit 0) prints shell-evalable ``KEY=value`` lines to
stdout (values quoted via ``shlex.quote`` so whitespace and shell
metacharacters survive). The required fields come first in fixed
order; any extra string fields follow alphabetically:

    LEAD_AGENT=crystallize-test
    FINISH_REPORT_PATH=runtime/harden/update-foo/reports/report.md
    TICKET_ID=task-42

Non-string frontmatter values (lists, mappings, numbers, bools) are
silently dropped -- only strings round-trip cleanly through ``eval``.
Extra string keys must be valid POSIX shell identifiers (so the
``KEY=value`` line a downstream ``eval`` consumes actually creates a
variable instead of being parsed as a command). A key like
``staged-inputs`` fails loud rather than silently disappearing.

On any failure -- no glob match, multiple glob matches, file missing,
no/broken frontmatter, any required field missing, wrong type, empty
string, or an extra key that isn't a valid shell identifier -- prints a
human-readable error to stderr and exits 1.
"""

from __future__ import annotations

import argparse
import glob
import re
import shlex
import sys
from pathlib import Path
from typing import Any

import yaml

_REQUIRED_FIELDS = ("lead_agent", "finish_report_path")
_SHELL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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
    """Return all top-level string fields after validating the required ones.

    Required fields (``lead_agent``, ``finish_report_path``) must be present,
    string-typed, and non-empty -- any violation raises ``ValueError``.
    Beyond those, all other top-level string-valued keys are passed
    through; non-string values are silently dropped. Extra keys must
    also be valid POSIX shell identifiers (``[A-Za-z_][A-Za-z0-9_]*``)
    so the downstream ``eval`` actually defines a variable rather than
    silently parsing the rendered line as a command lookup.
    """
    if not task_file.is_file():
        raise ValueError(f"task file not found: {task_file}")
    frontmatter = _split_frontmatter(task_file.read_text(encoding="utf-8"))
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
    result = {
        key: value
        for key, value in frontmatter.items()
        if isinstance(value, str) and value
    }
    for key in result:
        if key in _REQUIRED_FIELDS:
            continue
        if not _SHELL_IDENTIFIER_RE.match(key):
            raise ValueError(
                f"frontmatter key `{key}` is not a valid shell identifier "
                f"(must match {_SHELL_IDENTIFIER_RE.pattern}); rename it "
                f"using snake_case so downstream `eval` can consume the "
                f"rendered KEY=value line."
            )
    return result


def _render(fields: dict[str, str]) -> str:
    extras = sorted(key for key in fields if key not in _REQUIRED_FIELDS)
    ordered = [*_REQUIRED_FIELDS, *extras]
    lines = [
        f"{key.upper()}={shlex.quote(fields[key])}" for key in ordered if key in fields
    ]
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
