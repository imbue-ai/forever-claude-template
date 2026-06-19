"""Shell-aware parsing of ``tk``/``ticket`` command invocations.

The chat progress view's tooling needs to understand ``tk`` invocations that
appear inside a Bash command -- which subcommand ran (``start`` / ``close`` /
``create`` ...), with which arguments, and whether the invocation was chained
with or decorated by other commands. Doing that with regexes is unreliable: a
``tk close`` mentioned inside a quoted summary, an operator that lives inside a
quoted string, an escaped quote inside a ``--step`` title, or the ``--flag=value``
form all defeat a naive pattern. This module instead tokenizes the command with
``shlex`` (a real shell-aware lexer), so quoting, escapes, comments, env-var
prefixes, and operators are all interpreted the way a shell would.

``shlex`` treats a bare newline as ordinary whitespace, so unquoted newlines
(which a shell honours as command separators) are normalised to ``;`` before
tokenizing -- that is the lexer's one blind spot here, and the only place the
raw string is scanned char by char.

This module is intentionally **stdlib-only**: the PreToolUse gate hook
(``scripts/claude_tk_standalone_check.py``) imports it under a bare ``python3``
with no virtualenv, so it must not pull in any third-party dependency. That is
also why the records below are ``typing.NamedTuple`` rather than a pydantic
model.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence
from typing import NamedTuple

# Characters shlex returns as runs of "punctuation" tokens (operators) when
# ``punctuation_chars`` is enabled: command separators and redirects.
_PUNCT = "();<>|&"

# A leading ``VAR=value`` assignment. It is benign before a command (the shell
# still runs the command in place), so it is skipped when locating the verb.
_ENV_ASSIGN = re.compile(r"^[A-Za-z_]\w*=")

# The command names that identify a tk invocation, after stripping any path
# prefix (``vendor/tk/ticket`` -> ``ticket``).
_TK_BASENAMES = ("tk", "ticket")


class CommandSegment(NamedTuple):
    """One command in a Bash command line, split out at control operators.

    ``words`` are the segment's word tokens (operators excluded). ``has_redirect``
    is true when an output/input redirect (``>``, ``>>``, ``2>``, ``&>``,
    ``<``, ...) decorates the segment. When the segment is a ``tk``/``ticket``
    invocation, ``tk_verb`` is its subcommand (``start``, ``close``, ``create``,
    ...) and ``tk_args`` are the tokens that follow the verb; otherwise
    ``tk_verb`` is ``None`` and ``tk_args`` is empty.
    """

    words: tuple[str, ...]
    has_redirect: bool
    tk_verb: str | None
    tk_args: tuple[str, ...]


class ParsedCommand(NamedTuple):
    """A Bash command line tokenized and split into its command segments."""

    segments: tuple[CommandSegment, ...]


def parse_command(command: str) -> ParsedCommand | None:
    """Tokenize ``command`` and split it into command segments.

    Returns ``None`` when the command cannot be tokenized (e.g. unbalanced
    quotes) -- callers treat that as "nothing to act on". Always returns at
    least one (possibly empty) segment otherwise.
    """
    tokens = _tokenize(_newlines_to_semicolons(command.strip()))
    if tokens is None:
        return None

    segments: list[CommandSegment] = []
    words: list[str] = []
    has_redirect = False
    for tok in tokens:
        if _is_control(tok):
            segments.append(_classify_segment(tuple(words), has_redirect))
            words = []
            has_redirect = False
        elif _is_redirect(tok):
            # A redirect decorates the current command rather than separating
            # it from the next, so it is not a segment boundary.
            has_redirect = True
        else:
            words.append(tok)
    segments.append(_classify_segment(tuple(words), has_redirect))
    return ParsedCommand(segments=tuple(segments))


def flag_values(args: Sequence[str], flag: str) -> list[str]:
    """Every value passed to ``flag`` within ``args``, in order.

    Handles both the separated form (``--step "title"`` -> two tokens) and the
    joined form (``--step="title"`` -> one token). Quote stripping has already
    been done by the lexer, so the returned values are the literal argument
    text.
    """
    prefix = flag + "="
    values: list[str] = []
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == flag:
            if i + 1 < len(args):
                values.append(args[i + 1])
                i += 2
                continue
        elif tok.startswith(prefix):
            values.append(tok[len(prefix) :])
        i += 1
    return values


def extract_create_titles(command: str) -> list[str]:
    """Titles created by ``tk create --step "<title>"`` invocations in a full
    Bash command, in order.

    Returns ``[]`` when the command contains no such invocation. A batch of
    creates joined by ``&&`` / ``;`` / newlines yields every title; a ``--step``
    that merely appears inside another command's quoted argument (e.g. an
    ``echo`` or a commit message) yields nothing, because it is not the first
    word of a ``tk``/``ticket`` command segment.
    """
    # Cheap guard: a create that declares a step always contains the literal
    # ``--step``; skip the lexer for the overwhelmingly common command that does
    # not.
    if "--step" not in command:
        return []
    parsed = parse_command(command)
    if parsed is None:
        return []
    titles: list[str] = []
    for segment in parsed.segments:
        if segment.tk_verb == "create":
            titles.extend(flag_values(segment.tk_args, "--step"))
    return titles


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


def _classify_segment(words: tuple[str, ...], has_redirect: bool) -> CommandSegment:
    """Build a :class:`CommandSegment`, recognizing a tk/ticket invocation.

    Skips a leading run of ``VAR=value`` env assignments, accepts an explicit
    path prefix (``vendor/tk/ticket``) and the ``super`` plugin-bypass form, and
    records the subcommand verb plus the tokens that follow it.
    """
    i = 0
    while i < len(words) and _ENV_ASSIGN.match(words[i]):
        i += 1
    tk_verb: str | None = None
    tk_args: tuple[str, ...] = ()
    if i < len(words):
        base = words[i].rsplit("/", 1)[-1]
        if base in _TK_BASENAMES:
            j = i + 1
            if j < len(words) and words[j] == "super":
                j += 1
            if j < len(words):
                tk_verb = words[j]
                tk_args = words[j + 1 :]
    return CommandSegment(
        words=words, has_redirect=has_redirect, tk_verb=tk_verb, tk_args=tk_args
    )
