#!/usr/bin/env python3
"""Decide whether a Bash command is a non-standalone `tk start`/`tk close`.

Takes the command as its single positional argument (passed by
claude_tk_standalone.sh). Exits 0 to allow; exits 2 with a guiding stderr
message to BLOCK. Only `start` and `close` are in scope -- `create` is exempt.
See the wrapper for the why.

The command is tokenized with `shlex` (a real shell-aware lexer) rather than
matched with regexes, so quoting, comments, env-var prefixes, and operators are
all interpreted the way a shell would: a `tk close` summary in quotes, or any
string that merely mentions `tk close`, stays inside a single token and never
trips the operator checks. `shlex` treats a bare newline as ordinary
whitespace, so unquoted newlines (which a shell would honour as command
separators) are normalised to `;` before tokenizing -- that is the lexer's one
blind spot for our purposes, and the only place we still scan the raw string.
"""

import re
import shlex
import sys

# Characters shlex returns as runs of "punctuation" tokens (operators).
_PUNCT = "();<>|&"
# A leading `VAR=value` assignment -- benign before a tk command (the
# `Updated ... -> ...` line still prints at its normal position), so allowed.
_ENV_ASSIGN = re.compile(r"^[A-Za-z_]\w*=")

_BEFORE = "another command runs before it (for example a leading `cd`)"
_REDIRECT = "its output is redirected (`>`, `>>`, `2>`, `&>`, `</dev/null`, ...)"
_CHAIN = "it is chained with or backgrounded by another command (`&&`, `||`, `;`, `|`, `&`, or a newline)"


def _newlines_to_semicolons(cmd: str) -> str:
    """Replace unquoted newlines with `;` so they read as command separators.

    A newline inside quotes (e.g. a multi-line close summary) is preserved.
    Backslash escapes the next char outside single quotes, matching the shell.
    A `#` that begins a word (at the start, or after whitespace or a shell
    metacharacter) starts a comment that the shell ends at the newline, so the
    comment body is dropped here too -- otherwise shlex (which has no newlines
    left to terminate the comment on) would let it swallow the injected `;` and
    the command that follows, hiding a real second command from the checks. A
    `#` in the middle of a word (`a#b`) is a literal, matching the shell.
    """
    # Characters after which a `#` begins a new word (so starts a comment).
    word_boundary = set(" \t\n" + _PUNCT)
    out: list[str] = []
    quote: str | None = None
    escaped = False
    in_comment = False
    prev: str | None = None
    for ch in cmd:
        if in_comment:
            # The shell ends a comment at the newline; emit the separator and
            # resume normal scanning on the next line.
            if ch == "\n":
                in_comment = False
                out.append(";")
                prev = "\n"
            continue
        if escaped:
            out.append(ch)
            escaped = False
            prev = ch
            continue
        if quote != "'" and ch == "\\":
            out.append(ch)
            escaped = True
            prev = ch
            continue
        if quote is not None:
            out.append(ch)
            if ch == quote:
                quote = None
            prev = ch
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            prev = ch
            continue
        if ch == "#" and (prev is None or prev in word_boundary):
            in_comment = True
            continue
        out.append(";" if ch == "\n" else ch)
        prev = ch
    return "".join(out)


def _tokenize(cmd: str) -> list[str] | None:
    """Shell-tokenize `cmd`, or None if it cannot be parsed (unbalanced quotes)."""
    lexer = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return None


def _is_punct(tok: str) -> bool:
    return bool(tok) and all(ch in _PUNCT for ch in tok)


def _is_redirect(tok: str) -> bool:
    return _is_punct(tok) and ("<" in tok or ">" in tok)


def _is_control(tok: str) -> bool:
    # A control operator (`;`, `|`, `||`, `&`, `&&`, `(`, `)`) separates
    # commands. Redirects (`>`, `>&`, ...) are NOT separators -- they belong to
    # the command they decorate -- so they are excluded here.
    return _is_punct(tok) and not _is_redirect(tok)


def _is_tk_lifecycle(segment: list[str]) -> bool:
    """True if `segment` is a `tk`/`ticket` `start`/`close` command.

    Skips a leading run of `VAR=value` env assignments, accepts an explicit
    path prefix (`vendor/tk/ticket`) and the `super` plugin-bypass form.
    """
    i = 0
    while i < len(segment) and _ENV_ASSIGN.match(segment[i]):
        i += 1
    if i >= len(segment):
        return False
    base = segment[i].rsplit("/", 1)[-1]
    if base not in ("tk", "ticket"):
        return False
    j = i + 1
    if j < len(segment) and segment[j] == "super":
        j += 1
    return j < len(segment) and segment[j] in ("start", "close")


def classify(cmd: str) -> str | None:
    """Return the violation reason if `cmd` is a non-standalone tk start/close.

    Returns None when the command is allowed: either it is not a tk start/close
    at all (including `tk create`, or a non-tk command that merely mentions a tk
    verb inside a quoted string), or it is a clean standalone start/close.
    """
    tokens = _tokenize(_newlines_to_semicolons(cmd.strip()))
    if tokens is None:
        return None

    # Split the token stream into commands at control operators.
    segments: list[list[str]] = [[]]
    for tok in tokens:
        if _is_control(tok):
            segments.append([])
        else:
            segments[-1].append(tok)

    if not any(_is_tk_lifecycle(seg) for seg in segments):
        return None

    if not _is_tk_lifecycle(segments[0]):
        # A tk start/close exists, but something else is the first command.
        return _BEFORE
    if any(_is_redirect(tok) for tok in segments[0]):
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
