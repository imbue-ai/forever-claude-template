#!/usr/bin/env python3
"""Validate a proposed skill name against the agentskills.io rules.

Rules (per the spec):
- 1-64 characters.
- Lowercase ASCII letters, digits, and single hyphens only.
- No leading or trailing hyphens.
- No consecutive hyphens.

Exits 0 and prints ``ok`` when the name is valid; exits 1 with an
explanatory message to stderr otherwise.
"""

from __future__ import annotations

import re
import sys

_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MIN_LEN = 1
_MAX_LEN = 64


def _validate(name: str) -> str | None:
    if not (_MIN_LEN <= len(name) <= _MAX_LEN):
        return f"length must be {_MIN_LEN}-{_MAX_LEN} characters, got {len(name)}"
    if not _PATTERN.fullmatch(name):
        return (
            "must match ^[a-z0-9]+(?:-[a-z0-9]+)*$ -- lowercase letters/digits "
            "separated by single hyphens, no leading/trailing or consecutive hyphens"
        )
    return None


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: validate_skill_name.py <name>", file=sys.stderr)
        return 2
    error = _validate(sys.argv[1])
    if error is None:
        print("ok")
        return 0
    print(f"invalid skill name: {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
