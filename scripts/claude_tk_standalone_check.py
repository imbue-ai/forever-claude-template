#!/usr/bin/env python3
"""Decide whether a Bash command is a non-standalone `tk start`/`tk close`.

Reads the command from the TK_CMD env var (set by claude_tk_standalone.sh).
Exits 0 to allow; exits 2 with a guiding stderr message to BLOCK. Only `start`
and `close` are in scope -- `create` is exempt. See the wrapper for the why.
"""

import os
import re
import sys


def classify(cmd: str) -> str | None:
    """Return the violation reason if `cmd` is a non-standalone tk start/close.

    Returns None when the command is allowed: either it is not a tk start/close
    at all (including `tk create`, or a non-tk command that merely mentions a tk
    verb inside a quoted string), or it is a clean standalone start/close.
    """
    # 1. Strip quoted substrings (double- then single-quoted) so their contents
    #    are invisible to every check below. A close summary lives in quotes, so
    #    any operators or the literal words "tk close" inside it are neutralised
    #    (e.g. `git commit -m "tk close ..."` is left as plain work).
    dq = re.sub(r'"(?:\\.|[^"\\])*"', " ", cmd)
    dq = re.sub(r"'[^']*'", " ", dq)

    # 2. Is this actually a tk/ticket start or close? `super` is the
    #    plugin-bypass form. A leading non-word char (or string start) keeps
    #    `mytk`/`ticketing` from matching while still catching a path-prefixed
    #    `vendor/tk/ticket close`.
    if not re.search(r"(?:^|[^\w])(?:tk|ticket)\s+(?:super\s+)?(?:start|close)(?:\s|$)", dq):
        return None

    trimmed = dq.lstrip()

    # 3a. Must be the first/only command: the (de-quoted) command begins with
    #     the tk/ticket verb, optionally via an explicit path.
    if not re.match(r"(?:\S*/)?(?:tk|ticket)\s", trimmed):
        return "another command runs before it (for example a leading `cd`)"
    # 3b. No output redirection.
    if re.search(r"[<>]", dq):
        return "its output is redirected (`>`, `>>`, `2>`, `&>`, `</dev/null`, ...)"
    # 3c. No chaining/backgrounding with another command.
    if re.search(r"[&;|]|\n", dq):
        return "it is chained with or backgrounded by another command (`&&`, `||`, `;`, `|`, `&`, or a newline)"

    return None


def main() -> int:
    violation = classify(os.environ.get("TK_CMD", ""))
    if violation is None:
        return 0

    sys.stderr.write(
        "Blocked: run `tk start` / `tk close` as the ONLY command in the tool call -- "
        + violation + ".\n\n"
        "The chat progress view reads each step's structure and grouping from this "
        "command's visible output (the `Updated <id> -> <status>` line) and its position "
        "in the transcript. Chaining the command, prefixing a `cd`, or redirecting its "
        "output suppresses or mis-positions that, so the step stops grouping its work.\n\n"
        "tk works from any directory (it uses TICKETS_DIR), so you never need to `cd` first. "
        "Re-run with just the tk command on its own:\n"
        "  tk start <id>\n"
        '  tk close <id> "<summary>"\n'
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
