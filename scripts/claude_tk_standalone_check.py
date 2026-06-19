#!/usr/bin/env python3
"""Decide whether a Bash command is a non-standalone `tk start`/`tk close`.

Takes the command as its single positional argument (passed by
claude_tk_standalone.sh). Exits 0 to allow; exits 2 with a guiding stderr
message to BLOCK. Only `start` and `close` are in scope -- `create` is exempt.
See the wrapper for the why.

The command structure (which segments are tk invocations, whether one is
chained or redirected) comes from the shared `tk_command_parsing` parser, which
tokenizes with `shlex` (a real shell-aware lexer) rather than matching regexes,
so quoting, escapes, comments, env-var prefixes, and operators are interpreted
the way a shell would: a `tk close` summary in quotes, or any string that merely
mentions `tk close`, stays inside one token and never trips the operator checks.

This hook runs under a bare `python3` with no virtualenv (see the wrapper), so
it puts the parser lib's source directory on `sys.path` explicitly rather than
relying on an installed package; the lib is stdlib-only for the same reason.
"""

import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "libs" / "tk_command_parsing" / "src")
)

from tk_command_parsing.parser import parse_command

# The tk lifecycle subcommands this gate governs. `create` is exempt -- agents
# legitimately batch several `S1=$(tk create --step ...)` up front, and a create
# carries no positional transition the progress view must group around.
_LIFECYCLE_VERBS = ("start", "close")

_BEFORE = "another command runs before it (for example a leading `cd`)"
_REDIRECT = "its output is redirected (`>`, `>>`, `2>`, `&>`, `</dev/null`, ...)"
_CHAIN = "it is chained with or backgrounded by another command (`&&`, `||`, `;`, `|`, `&`, or a newline)"


def classify(cmd: str) -> str | None:
    """Return the violation reason if `cmd` is a non-standalone tk start/close.

    Returns None when the command is allowed: either it is not a tk start/close
    at all (including `tk create`, or a non-tk command that merely mentions a tk
    verb inside a quoted string), or it is a clean standalone start/close.
    """
    parsed = parse_command(cmd)
    if parsed is None:
        return None
    segments = parsed.segments

    if not any(seg.tk_verb in _LIFECYCLE_VERBS for seg in segments):
        return None

    if segments[0].tk_verb not in _LIFECYCLE_VERBS:
        # A tk start/close exists, but something else is the first command.
        return _BEFORE
    if segments[0].has_redirect:
        return _REDIRECT
    if len(segments) > 1:
        # A control operator split the stream, so another command runs
        # alongside the tk start/close (or it is backgrounded with `&`).
        return _CHAIN
    return None


def main(argv: list[str] | None = None) -> int:
    args = sys.argv if argv is None else argv
    command = args[1] if len(args) > 1 else ""
    violation = classify(command)
    if violation is None:
        return 0

    sys.stderr.write(
        "Blocked: run `tk start` / `tk close` as the ONLY command in the tool call -- "
        + violation
        + ".\n\n"
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
