"""Unit tests for the shell-aware tk command parser.

These pin the behaviour the gate hook (``scripts/claude_tk_standalone_check.py``)
relies on, plus the ``extract_create_titles`` / ``flag_values`` surface the
package exposes for callers that need step titles out of a ``tk create --step``
command. The cases that motivated moving off regexes -- quoted operators, escaped
quotes inside titles, a tk verb merely mentioned inside another command's
argument, and the ``--flag=value`` form -- are covered explicitly.
"""

from __future__ import annotations

from tk_command_parsing.parser import extract_create_titles, flag_values, parse_command


def _verbs(command: str) -> list[str | None]:
    parsed = parse_command(command)
    assert parsed is not None
    return [seg.tk_verb for seg in parsed.segments]


# --- segment / verb recognition ---


def test_standalone_invocation_is_a_single_segment() -> None:
    parsed = parse_command("tk start cod-step-x")
    assert parsed is not None
    assert len(parsed.segments) == 1
    assert parsed.segments[0].tk_verb == "start"
    assert parsed.segments[0].tk_args == ("cod-step-x",)
    assert parsed.segments[0].has_redirect is False


def test_env_prefix_path_prefix_and_super_are_seen_through() -> None:
    assert _verbs('TICKETS_DIR=/x tk close cod-step-x "s"') == ["close"]
    assert _verbs("A=1 B=2 tk start cod-step-x") == ["start"]
    assert _verbs('vendor/tk/ticket close cod-step-x "s"') == ["close"]
    assert _verbs("tk super close cod-step-x") == ["close"]


def test_quoted_operators_do_not_split_the_command() -> None:
    """Operators inside a quoted argument stay in one token -- a close summary
    that contains `;`, `&&`, `>` is still a single standalone command."""
    assert _verbs('tk close cod-step-x "did A; then B > C && D"') == ["close"]


def test_control_operators_split_segments() -> None:
    assert _verbs('tk close a "x" && tk start b') == ["close", "start"]
    assert _verbs("cd /x && tk close cod-step-x") == [None, "close"]


def test_redirect_is_recorded_not_a_separator() -> None:
    parsed = parse_command("tk start cod-step-x >/dev/null 2>&1")
    assert parsed is not None
    assert [seg.tk_verb for seg in parsed.segments] == ["start"]
    assert parsed.segments[0].has_redirect is True


def test_a_mentioned_verb_inside_a_quote_is_not_an_invocation() -> None:
    assert _verbs('git commit -m "tk close foo"') == [None]
    assert _verbs("echo 'run tk start later'") == [None]


def test_unbalanced_quotes_return_none() -> None:
    assert parse_command('tk close cod-step-x "unterminated') is None


# --- flag_values ---


def test_flag_values_handles_separated_and_joined_forms() -> None:
    assert flag_values(["--step", "a title", "--other", "x"], "--step") == ["a title"]
    assert flag_values(["--step=a title"], "--step") == ["a title"]
    assert flag_values(["--step", "first", "--step", "second"], "--step") == [
        "first",
        "second",
    ]
    assert flag_values(["--no-step-here"], "--step") == []


# --- extract_create_titles ---


def test_extract_titles_from_batched_creates_including_parens() -> None:
    command = (
        'S1=$(tk create --step "Explore the directory"); '
        'S2=$(tk create --step "Analyze build process (vite config)"); '
        'S3=$(tk create --step "Examine backend")'
    )
    assert extract_create_titles(command) == [
        "Explore the directory",
        "Analyze build process (vite config)",
        "Examine backend",
    ]


def test_extract_titles_ignores_non_step_creates_and_mentions() -> None:
    assert extract_create_titles('tk create "Regular ticket"') == []
    assert extract_create_titles('git commit -m "tk close foo"') == []
    assert extract_create_titles("tk ls --only-steps") == []
    # A --step mentioned inside an unrelated command's quoted argument is not a
    # real create invocation. The old regex extracted "real title" here.
    assert (
        extract_create_titles("echo \"remember tk create --step 'real title'\"") == []
    )


def test_extract_titles_handles_single_quotes_and_super() -> None:
    assert extract_create_titles(
        "S1=$(tk super create --step 'Quoted with single')"
    ) == ["Quoted with single"]


def test_extract_titles_preserves_escaped_quotes_in_title() -> None:
    """An escaped double-quote inside a double-quoted title is part of the
    title. The old regex truncated at the first inner quote."""
    assert extract_create_titles('tk create --step "fix the \\"foo\\" bug"') == [
        'fix the "foo" bug'
    ]


def test_extract_titles_handles_joined_step_form() -> None:
    """The `--step=value` form yields the title; the old regex missed it."""
    assert extract_create_titles('tk create --step="equals form"') == ["equals form"]
