"""Unit tests for the window error watcher's pure core.

The `mngr` argv builders are additionally confronted with the live
`imbue.mngr.main.cli` tree via `assert_mngr_argv_valid`, so a vendor/mngr rename
of the `list`/`message` subcommand or one of its flags fails here at merge time.
"""

import json
import random
from collections.abc import Mapping, Sequence
from typing import NamedTuple

from mngr_cli_contract.contract import assert_mngr_argv_valid

from error_watcher.watcher import (
    DEFAULT_ERROR_PATTERN,
    AgentSummary,
    CommandResult,
    build_list_command,
    build_message_command,
    choose_recipient,
    compile_error_pattern,
    format_alert,
    mark_alerted,
    match_lines,
    parse_agent_summaries,
    run_one_poll,
    select_messageable_names,
    unseen_matches,
)


def test_match_lines_is_case_insensitive() -> None:
    text = "all good\nError: boom\nEXCEPTION raised\nstill fine"
    assert match_lines(text, DEFAULT_ERROR_PATTERN) == [
        "Error: boom",
        "EXCEPTION raised",
    ]


def test_match_lines_matches_traceback_exception() -> None:
    text = "Traceback (most recent call last):\n  File ...\nValueError: bad\nException: nope"
    matched = match_lines(text, DEFAULT_ERROR_PATTERN)
    assert "Exception: nope" in matched


def test_match_lines_returns_empty_for_clean_output() -> None:
    assert (
        match_lines("compiled successfully\nall tests passed\n", DEFAULT_ERROR_PATTERN)
        == []
    )


def test_match_lines_naively_matches_benign_substrings() -> None:
    # v1 is deliberately naive: "0 errors" and "ErrorBoundary" both contain
    # "error", so they match. This documents the spec's stated non-goal.
    text = "0 errors\nrendered <ErrorBoundary>\nok"
    assert match_lines(text, DEFAULT_ERROR_PATTERN) == [
        "0 errors",
        "rendered <ErrorBoundary>",
    ]


def test_unseen_matches_returns_fresh_lines_without_recording_them() -> None:
    seen: dict[str, set[str]] = {}
    # unseen_matches is read-only: it must NOT mark the line as alerted, so a
    # line is only suppressed once mark_alerted records a delivered alert.
    assert unseen_matches("svc-web", ["Error: boom"], seen) == ["Error: boom"]
    assert seen == {}


def test_unseen_matches_suppresses_lines_after_they_are_marked_alerted() -> None:
    seen: dict[str, set[str]] = {}
    fresh = unseen_matches("svc-web", ["Error: boom"], seen)
    mark_alerted({"svc-web": fresh}, seen)
    # The same error still on screen on the next poll must not re-alert.
    assert unseen_matches("svc-web", ["Error: boom"], seen) == []


def test_unseen_matches_returns_only_the_newly_appeared_line() -> None:
    seen: dict[str, set[str]] = {}
    mark_alerted({"svc-web": ["Error: boom"]}, seen)
    assert unseen_matches("svc-web", ["Error: boom", "Exception: later"], seen) == [
        "Exception: later"
    ]


def test_unseen_matches_tracks_windows_independently() -> None:
    seen: dict[str, set[str]] = {}
    mark_alerted({"svc-web": ["Error: boom"]}, seen)
    # The same text in a different window is new for that window.
    assert unseen_matches("svc-api", ["Error: boom"], seen) == ["Error: boom"]


def test_unseen_matches_deduplicates_within_a_single_capture() -> None:
    seen: dict[str, set[str]] = {}
    assert unseen_matches("svc-web", ["Error: boom", "Error: boom"], seen) == [
        "Error: boom"
    ]


def test_format_alert_includes_session_window_and_line() -> None:
    message = format_alert("agent-session", {"svc-web": ["Error: boom"]})
    assert "agent-session" in message
    assert "svc-web" in message
    assert "Error: boom" in message


def test_format_alert_batches_multiple_windows_into_one_message() -> None:
    message = format_alert(
        "agent-session",
        {"svc-web": ["Error: boom"], "svc-api": ["Exception: a", "Exception: b"]},
    )
    assert "svc-web" in message
    assert "svc-api" in message
    assert "Exception: a | Exception: b" in message


def test_format_alert_truncates_overlong_lines() -> None:
    long_line = "Error " + "x" * 1000
    message = format_alert("agent-session", {"svc-web": [long_line]})
    assert "..." in message
    assert len(long_line) not in {len(part) for part in message.splitlines()}


def test_list_command_is_accepted_by_live_cli() -> None:
    argv = build_list_command()
    assert argv == ["mngr", "list", "--format", "json"]
    assert_mngr_argv_valid(argv)


def test_message_command_is_accepted_by_live_cli() -> None:
    argv = build_message_command("demo-agent", "something errored")
    assert argv == ["mngr", "message", "demo-agent", "-m", "something errored"]
    assert_mngr_argv_valid(argv)


def test_parse_agent_summaries_reads_name_and_state() -> None:
    payload = json.dumps(
        {
            "agents": [
                {
                    "resource_type": "agent",
                    "name": "agent-web",
                    "type": "claude",
                    "state": "RUNNING",
                },
                {"name": "agent-api", "type": "claude", "state": "STOPPED"},
            ],
            "errors": [],
        }
    )
    assert parse_agent_summaries(payload) == [
        AgentSummary(name="agent-web", state="RUNNING"),
        AgentSummary(name="agent-api", state="STOPPED"),
    ]


def test_parse_agent_summaries_skips_agents_missing_name_or_state() -> None:
    payload = json.dumps(
        {
            "agents": [
                {"name": "agent-web", "state": "RUNNING"},
                {"name": "", "state": "RUNNING"},
                {"name": "agent-api"},
                "not-a-dict",
            ]
        }
    )
    assert parse_agent_summaries(payload) == [
        AgentSummary(name="agent-web", state="RUNNING")
    ]


def test_parse_agent_summaries_returns_empty_on_malformed_json() -> None:
    assert parse_agent_summaries("this is not json") == []


def test_parse_agent_summaries_returns_empty_when_not_an_object() -> None:
    assert parse_agent_summaries("[1, 2, 3]") == []


def test_parse_agent_summaries_returns_empty_when_agents_not_a_list() -> None:
    assert parse_agent_summaries(json.dumps({"agents": "nope"})) == []


def test_choose_recipient_is_deterministic_for_a_seeded_rng() -> None:
    # random.Random(0).choice(["alpha", "beta", "gamma"]) is "beta".
    assert choose_recipient(["alpha", "beta", "gamma"], random.Random(0)) == "beta"


def test_choose_recipient_returns_none_for_empty_pool() -> None:
    assert choose_recipient([], random.Random(0)) is None


def test_select_messageable_names_excludes_only_stopped_agents() -> None:
    agents = [
        AgentSummary(name="run", state="RUNNING"),
        AgentSummary(name="wait", state="WAITING"),
        AgentSummary(name="stop", state="STOPPED"),
    ]
    assert select_messageable_names(agents) == ["run", "wait"]


def test_select_messageable_names_empty_when_all_stopped() -> None:
    assert select_messageable_names([AgentSummary(name="stop", state="STOPPED")]) == []


def test_compile_error_pattern_defaults_when_no_override() -> None:
    assert compile_error_pattern(None) is DEFAULT_ERROR_PATTERN
    assert compile_error_pattern("") is DEFAULT_ERROR_PATTERN


def test_compile_error_pattern_uses_case_insensitive_override() -> None:
    pattern = compile_error_pattern("panic")
    assert pattern.search("PANIC: kernel")
    assert pattern.search("everything is fine") is None


def test_compile_error_pattern_falls_back_on_invalid_regex() -> None:
    assert compile_error_pattern("[unclosed") is DEFAULT_ERROR_PATTERN


# Two agents that can both receive a message; with random.Random(0) the chosen
# recipient over ["agent-web", "agent-api"] is deterministically "agent-api".
_TWO_MESSAGEABLE_AGENTS = json.dumps(
    {
        "agents": [
            {"name": "agent-web", "type": "claude", "state": "RUNNING"},
            {"name": "agent-api", "type": "claude", "state": "WAITING"},
        ],
        "errors": [],
    }
)

_ONLY_STOPPED_AGENT = json.dumps(
    {
        "agents": [{"name": "agent-web", "type": "claude", "state": "STOPPED"}],
        "errors": [],
    }
)


class _FakeCommandRunner(NamedTuple):
    """Drives run_one_poll without real tmux/mngr by mapping each argv to a canned result.

    Records every `mngr message` argv in `message_sends` so a test can assert
    exactly one batched alert was sent and to whom. Windows named in
    `failing_windows` return a non-zero capture, simulating a window that
    vanished mid-poll.
    """

    session: str
    windows: tuple[str, ...]
    pane_text_by_window: Mapping[str, str]
    list_stdout: str
    message_sends: list[list[str]]
    failing_windows: frozenset[str] = frozenset()
    send_fails: bool = False

    def __call__(self, command: Sequence[str]) -> CommandResult:
        argv = list(command)
        if argv == ["tmux", "display-message", "-p", "#S"]:
            return CommandResult(0, self.session + "\n", "")
        if argv[:3] == ["tmux", "list-windows", "-t"]:
            return CommandResult(0, "\n".join(self.windows) + "\n", "")
        if argv[:2] == ["tmux", "capture-pane"]:
            window = argv[argv.index("-t") + 1].split(":", 1)[1]
            if window in self.failing_windows:
                return CommandResult(1, "", f"can't find window: {window}")
            return CommandResult(0, self.pane_text_by_window.get(window, ""), "")
        if argv == ["mngr", "list", "--format", "json"]:
            return CommandResult(0, self.list_stdout, "")
        if argv[:2] == ["mngr", "message"]:
            self.message_sends.append(argv)
            if self.send_fails:
                return CommandResult(1, "", "delivery failed")
            return CommandResult(0, "", "")
        return CommandResult(127, "", f"unexpected command: {argv}")


def test_run_one_poll_sends_one_alert_for_a_new_error() -> None:
    sends: list[list[str]] = []
    runner = _FakeCommandRunner(
        session="agent-session",
        windows=("svc-web", "svc-api", "svc-error-watcher", "bootstrap"),
        pane_text_by_window={
            "svc-web": "Traceback (most recent call last):\n  File ...\nException: boom",
            "svc-api": "all healthy",
            # The watcher's own alert text contains "error"; it must be skipped.
            "svc-error-watcher": "Possible error/exception detected by error-watcher ...",
            "bootstrap": "services reconciled",
        },
        list_stdout=_TWO_MESSAGEABLE_AGENTS,
        message_sends=sends,
    )
    recipient = run_one_poll(runner, {}, random.Random(0), DEFAULT_ERROR_PATTERN)
    assert recipient == "agent-api"
    # Batched: a single message even though only one window matched here.
    assert len(sends) == 1
    argv = sends[0]
    assert argv[:3] == ["mngr", "message", "agent-api"]
    body = argv[-1]
    assert "svc-web" in body
    assert "Exception: boom" in body
    # The own-window match must not leak into the alert (REQ-SCAN-2).
    assert "svc-error-watcher" not in body


def test_run_one_poll_batches_multiple_windows_into_one_message() -> None:
    sends: list[list[str]] = []
    runner = _FakeCommandRunner(
        session="agent-session",
        windows=("svc-web", "svc-api"),
        pane_text_by_window={
            "svc-web": "Exception: boom",
            "svc-api": "ERROR: kaput",
        },
        list_stdout=_TWO_MESSAGEABLE_AGENTS,
        message_sends=sends,
    )
    run_one_poll(runner, {}, random.Random(0), DEFAULT_ERROR_PATTERN)
    assert len(sends) == 1
    body = sends[0][-1]
    assert "svc-web" in body
    assert "svc-api" in body


def test_run_one_poll_does_not_realert_on_a_static_error() -> None:
    sends: list[list[str]] = []
    runner = _FakeCommandRunner(
        session="agent-session",
        windows=("svc-web",),
        pane_text_by_window={"svc-web": "Exception: boom"},
        list_stdout=_TWO_MESSAGEABLE_AGENTS,
        message_sends=sends,
    )
    seen: dict[str, set[str]] = {}
    assert (
        run_one_poll(runner, seen, random.Random(0), DEFAULT_ERROR_PATTERN)
        == "agent-api"
    )
    # Same error still on screen next poll: no second alert (REQ-MATCH-3).
    assert run_one_poll(runner, seen, random.Random(0), DEFAULT_ERROR_PATTERN) is None
    assert len(sends) == 1


def test_run_one_poll_skips_when_no_messageable_agent() -> None:
    sends: list[list[str]] = []
    runner = _FakeCommandRunner(
        session="agent-session",
        windows=("svc-web",),
        pane_text_by_window={"svc-web": "Exception: boom"},
        list_stdout=_ONLY_STOPPED_AGENT,
        message_sends=sends,
    )
    assert run_one_poll(runner, {}, random.Random(0), DEFAULT_ERROR_PATTERN) is None
    assert sends == []


def test_run_one_poll_realerts_once_an_agent_becomes_messageable() -> None:
    # An undelivered alert must not mark the error as seen, so the still-visible
    # error is alerted on a later poll once a messageable agent exists. Two
    # runners share `seen` and `sends`; only the messageable set differs.
    sends: list[list[str]] = []
    seen: dict[str, set[str]] = {}
    windows = ("svc-web",)
    pane_text = {"svc-web": "Exception: boom"}
    only_stopped = _FakeCommandRunner(
        session="agent-session",
        windows=windows,
        pane_text_by_window=pane_text,
        list_stdout=_ONLY_STOPPED_AGENT,
        message_sends=sends,
    )
    now_messageable = _FakeCommandRunner(
        session="agent-session",
        windows=windows,
        pane_text_by_window=pane_text,
        list_stdout=_TWO_MESSAGEABLE_AGENTS,
        message_sends=sends,
    )
    assert (
        run_one_poll(only_stopped, seen, random.Random(0), DEFAULT_ERROR_PATTERN)
        is None
    )
    assert sends == []
    # The error is still on screen and an agent is now reachable: it must alert.
    assert (
        run_one_poll(now_messageable, seen, random.Random(0), DEFAULT_ERROR_PATTERN)
        == "agent-api"
    )
    assert len(sends) == 1
    assert "Exception: boom" in sends[0][-1]


def test_run_one_poll_realerts_after_a_failed_send() -> None:
    # A send that fails (mngr message returns non-zero) must not mark the error
    # as seen, so the next poll retries it rather than dropping it silently.
    sends: list[list[str]] = []
    seen: dict[str, set[str]] = {}
    windows = ("svc-web",)
    pane_text = {"svc-web": "Exception: boom"}
    failing = _FakeCommandRunner(
        session="agent-session",
        windows=windows,
        pane_text_by_window=pane_text,
        list_stdout=_TWO_MESSAGEABLE_AGENTS,
        message_sends=sends,
        send_fails=True,
    )
    succeeding = _FakeCommandRunner(
        session="agent-session",
        windows=windows,
        pane_text_by_window=pane_text,
        list_stdout=_TWO_MESSAGEABLE_AGENTS,
        message_sends=sends,
    )
    assert run_one_poll(failing, seen, random.Random(0), DEFAULT_ERROR_PATTERN) is None
    # The failed send is still recorded as an attempt, but the error is not
    # marked seen, so the next poll retries and this time succeeds.
    assert len(sends) == 1
    assert (
        run_one_poll(succeeding, seen, random.Random(0), DEFAULT_ERROR_PATTERN)
        == "agent-api"
    )
    assert len(sends) == 2


def test_run_one_poll_tolerates_a_window_capture_failure() -> None:
    sends: list[list[str]] = []
    runner = _FakeCommandRunner(
        session="agent-session",
        windows=("svc-broken", "svc-web"),
        pane_text_by_window={"svc-web": "Exception: boom"},
        list_stdout=_TWO_MESSAGEABLE_AGENTS,
        message_sends=sends,
        failing_windows=frozenset({"svc-broken"}),
    )
    recipient = run_one_poll(runner, {}, random.Random(0), DEFAULT_ERROR_PATTERN)
    assert recipient == "agent-api"
    assert len(sends) == 1
    assert "svc-web" in sends[0][-1]
    assert "svc-broken" not in sends[0][-1]


def test_run_one_poll_ignores_errors_in_its_own_window() -> None:
    sends: list[list[str]] = []
    runner = _FakeCommandRunner(
        session="agent-session",
        windows=("svc-error-watcher",),
        pane_text_by_window={"svc-error-watcher": "Exception: boom in my own alert"},
        list_stdout=_TWO_MESSAGEABLE_AGENTS,
        message_sends=sends,
    )
    assert run_one_poll(runner, {}, random.Random(0), DEFAULT_ERROR_PATTERN) is None
    assert sends == []


def test_run_one_poll_returns_none_when_session_cannot_be_determined() -> None:
    sends: list[list[str]] = []
    runner = _FakeCommandRunner(
        session="",
        windows=("svc-web",),
        pane_text_by_window={"svc-web": "Exception: boom"},
        list_stdout=_TWO_MESSAGEABLE_AGENTS,
        message_sends=sends,
    )
    assert run_one_poll(runner, {}, random.Random(0), DEFAULT_ERROR_PATTERN) is None
    assert sends == []
