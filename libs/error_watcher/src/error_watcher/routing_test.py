"""Unit and integration tests for the routing layer.

Three layers of coverage:
- the pure match/dedup core functions;
- `ErrorRouter` in isolation, driven by scripted fake input/output doubles so the
  cross-poll logic (mark-seen-only-on-delivery, pruning) is exercised without I/O;
- `ErrorRouter` wired to the real tmux input and mngr output over a
  `FakeCommandRunner`, so a full poll is exercised end to end.
"""

import random

from error_watcher.inputs import ErrorReading, ErrorSource
from error_watcher.mngr_agent_error_output import RandomMngrAgentErrorOutput
from error_watcher.routing import (
    DEFAULT_ERROR_PATTERN,
    MAX_SEEN_KEYS_PER_SOURCE,
    ErrorRouter,
    compile_error_pattern,
    dedup_key,
    mark_alerted,
    match_lines,
    prune_seen_sources,
    unseen_matches,
)
from error_watcher.testing import (
    FakeCommandRunner,
    RecordingErrorOutput,
    SequencedErrorInput,
)
from error_watcher.tmux_window_error_input import TmuxWindowErrorInput

_OWN_WINDOW = "svc-error-watcher"

# Two agents that can both receive a message; with random.Random(0) the chosen
# recipient over ["agent-web", "agent-api"] is deterministically "agent-api".
_TWO_MESSAGEABLE_AGENTS = (
    '{"agents": ['
    '{"name": "agent-web", "type": "claude", "state": "RUNNING"},'
    '{"name": "agent-api", "type": "claude", "state": "WAITING"}'
    '], "errors": []}'
)

_ONLY_STOPPED_AGENT = '{"agents": [{"name": "agent-web", "type": "claude", "state": "STOPPED"}], "errors": []}'

_CLAUDE_AND_MAIN_AGENTS = (
    '{"agents": ['
    '{"name": "system-services", "type": "main", "state": "RUNNING"},'
    '{"name": "agent-claude", "type": "claude", "state": "RUNNING"}'
    '], "errors": []}'
)


def _integrated_router(runner: FakeCommandRunner, rng: random.Random) -> ErrorRouter:
    return ErrorRouter(
        TmuxWindowErrorInput(runner, _OWN_WINDOW),
        RandomMngrAgentErrorOutput(runner, rng),
        DEFAULT_ERROR_PATTERN,
    )


# --- Pure matching core ---


def test_match_lines_is_case_insensitive() -> None:
    text = "all good\nError: boom\nEXCEPTION raised\nstill fine"
    assert match_lines(text, DEFAULT_ERROR_PATTERN) == [
        "Error: boom",
        "EXCEPTION raised",
    ]


def test_match_lines_matches_traceback_exception() -> None:
    text = "Traceback (most recent call last):\n  File ...\nValueError: bad\nException: nope"
    assert "Exception: nope" in match_lines(text, DEFAULT_ERROR_PATTERN)


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


def test_dedup_key_collapses_digit_runs() -> None:
    assert dedup_key("[12:00:05] ERROR boom") == dedup_key("[12:00:10] ERROR boom")
    assert dedup_key("[12:00:05] ERROR boom") == "[#:#:#] ERROR boom"


def test_unseen_matches_returns_fresh_lines_without_recording_them() -> None:
    seen: dict[str, set[str]] = {}
    # unseen_matches is read-only: a line is only suppressed once mark_alerted
    # records a delivered alert.
    assert unseen_matches("svc-web", ["Error: boom"], seen) == ["Error: boom"]
    assert seen == {}


def test_unseen_matches_suppresses_lines_after_they_are_marked_alerted() -> None:
    seen: dict[str, set[str]] = {}
    fresh = unseen_matches("svc-web", ["Error: boom"], seen)
    mark_alerted({"svc-web": fresh}, seen)
    assert unseen_matches("svc-web", ["Error: boom"], seen) == []


def test_unseen_matches_returns_only_the_newly_appeared_line() -> None:
    seen: dict[str, set[str]] = {}
    mark_alerted({"svc-web": ["Error: boom"]}, seen)
    assert unseen_matches("svc-web", ["Error: boom", "Exception: later"], seen) == [
        "Exception: later"
    ]


def test_unseen_matches_tracks_sources_independently() -> None:
    seen: dict[str, set[str]] = {}
    mark_alerted({"svc-web": ["Error: boom"]}, seen)
    assert unseen_matches("svc-api", ["Error: boom"], seen) == ["Error: boom"]


def test_unseen_matches_deduplicates_within_a_single_capture() -> None:
    seen: dict[str, set[str]] = {}
    assert unseen_matches("svc-web", ["Error: boom", "Error: boom"], seen) == [
        "Error: boom"
    ]


def test_unseen_matches_collapses_re_timestamped_lines() -> None:
    seen: dict[str, set[str]] = {}
    fresh = unseen_matches("svc-web", ["[12:00:05] ERROR boom"], seen)
    mark_alerted({"svc-web": fresh}, seen)
    assert unseen_matches("svc-web", ["[12:00:10] ERROR boom"], seen) == []


def test_unseen_matches_keeps_errors_that_differ_in_text() -> None:
    seen: dict[str, set[str]] = {}
    mark_alerted({"svc-web": ["[12:00:05] ERROR boom"]}, seen)
    assert unseen_matches("svc-web", ["[12:00:10] ERROR kaboom"], seen) == [
        "[12:00:10] ERROR kaboom"
    ]


def test_mark_alerted_caps_keys_per_source() -> None:
    seen: dict[str, set[str]] = {}
    lines = [f"error {i}" for i in range(MAX_SEEN_KEYS_PER_SOURCE + 50)]
    mark_alerted({"svc-web": lines}, seen)
    assert len(seen["svc-web"]) <= MAX_SEEN_KEYS_PER_SOURCE


def test_prune_seen_sources_drops_state_for_closed_sources() -> None:
    seen: dict[str, set[str]] = {"svc-web": {"error #"}, "svc-gone": {"error #"}}
    prune_seen_sources(seen, ["svc-web", "svc-error-watcher"])
    assert set(seen) == {"svc-web"}


def test_prune_seen_sources_keeps_all_when_nothing_closed() -> None:
    seen: dict[str, set[str]] = {"svc-web": {"error #"}}
    prune_seen_sources(seen, ["svc-web", "svc-api"])
    assert set(seen) == {"svc-web"}


def test_compile_error_pattern_defaults_when_no_override() -> None:
    assert compile_error_pattern(None) is DEFAULT_ERROR_PATTERN
    assert compile_error_pattern("") is DEFAULT_ERROR_PATTERN


def test_compile_error_pattern_uses_case_insensitive_override() -> None:
    pattern = compile_error_pattern("panic")
    assert pattern.search("PANIC: kernel")
    assert pattern.search("everything is fine") is None


def test_compile_error_pattern_falls_back_on_invalid_regex() -> None:
    assert compile_error_pattern("[unclosed") is DEFAULT_ERROR_PATTERN


# --- ErrorRouter in isolation (scripted fake input/output) ---


def _reading(origin: str, **content_by_source: str) -> ErrorReading:
    return ErrorReading(
        origin=origin,
        sources=tuple(
            ErrorSource(name=name, content=content)
            for name, content in content_by_source.items()
        ),
    )


def test_router_returns_recipient_and_batches_sources_into_one_alert() -> None:
    error_input = SequencedErrorInput(
        [_reading("agent-session", svc_web="Exception: boom", svc_api="ERROR: kaput")]
    )
    output = RecordingErrorOutput(["agent-x"])
    recipient = ErrorRouter(error_input, output, DEFAULT_ERROR_PATTERN).run_once()
    assert recipient == "agent-x"
    assert len(output.delivered) == 1
    assert set(output.delivered[0].matches_by_source) == {"svc_web", "svc_api"}


def test_router_returns_none_and_does_not_deliver_for_empty_origin() -> None:
    error_input = SequencedErrorInput([_reading("", svc_web="Exception: boom")])
    output = RecordingErrorOutput(["agent-x"])
    assert ErrorRouter(error_input, output, DEFAULT_ERROR_PATTERN).run_once() is None
    assert output.delivered == []


def test_router_marks_seen_only_after_a_delivery_succeeds() -> None:
    # The same error is on screen for three polls. Delivery fails (None) on the
    # first poll, so the error must NOT be recorded and is retried on the second
    # poll, where delivery succeeds and it is recorded. The third poll suppresses
    # it. This is the lost-alert-ordering guarantee (REQ-MATCH-3) at the router.
    error_input = SequencedErrorInput(
        [_reading("agent-session", svc_web="Exception: boom")]
    )
    output = RecordingErrorOutput([None, "agent-x"])
    router = ErrorRouter(error_input, output, DEFAULT_ERROR_PATTERN)
    assert router.run_once() is None
    assert router.run_once() == "agent-x"
    assert router.run_once() is None
    # Delivered twice (poll 1 failed, poll 2 succeeded); poll 3 suppressed it so
    # the output was not asked again.
    assert len(output.delivered) == 2


def test_router_does_not_realert_a_static_error() -> None:
    error_input = SequencedErrorInput(
        [_reading("agent-session", svc_web="Exception: boom")]
    )
    output = RecordingErrorOutput(["agent-x"])
    router = ErrorRouter(error_input, output, DEFAULT_ERROR_PATTERN)
    assert router.run_once() == "agent-x"
    assert router.run_once() is None
    assert len(output.delivered) == 1


def test_router_realerts_a_reopened_source_after_it_was_pruned() -> None:
    # svc-web errors, then closes, then reopens with the same error. Because its
    # dedup state was pruned when it closed, the reopened error is genuinely new
    # again and re-alerts -- proving prune_seen_sources ran across polls.
    error_input = SequencedErrorInput(
        [
            _reading("agent-session", svc_web="Exception: boom", svc_api="all good"),
            _reading("agent-session", svc_api="all good"),
            _reading("agent-session", svc_web="Exception: boom", svc_api="all good"),
        ]
    )
    output = RecordingErrorOutput(["agent-x"])
    router = ErrorRouter(error_input, output, DEFAULT_ERROR_PATTERN)
    assert router.run_once() == "agent-x"  # poll 1: alert
    assert router.run_once() is None  # poll 2: svc-web closed, pruned
    assert router.run_once() == "agent-x"  # poll 3: svc-web reopened, re-alert
    assert len(output.delivered) == 2


def test_router_skips_pruning_when_a_read_returns_no_sources() -> None:
    # An empty read signals an input failure, not a source-less session, so dedup
    # state must survive it: the still-on-screen error is not re-alerted once the
    # input recovers.
    error_input = SequencedErrorInput(
        [
            _reading("agent-session", svc_web="Exception: boom"),
            ErrorReading(origin="agent-session", sources=()),
            _reading("agent-session", svc_web="Exception: boom"),
        ]
    )
    output = RecordingErrorOutput(["agent-x"])
    router = ErrorRouter(error_input, output, DEFAULT_ERROR_PATTERN)
    assert router.run_once() == "agent-x"  # poll 1: alert + record
    assert router.run_once() is None  # poll 2: empty read, state preserved
    assert router.run_once() is None  # poll 3: same error still suppressed
    assert len(output.delivered) == 1


# --- ErrorRouter wired to the real tmux input + mngr output (end to end) ---


def test_poll_sends_one_alert_for_a_new_error() -> None:
    sends: list[list[str]] = []
    runner = FakeCommandRunner(
        session="agent-session",
        windows=("svc-web", "svc-api", _OWN_WINDOW, "bootstrap"),
        pane_text_by_window={
            "svc-web": "Traceback (most recent call last):\n  File ...\nException: boom",
            "svc-api": "all healthy",
            _OWN_WINDOW: "Possible error/exception detected by error-watcher ...",
            "bootstrap": "services reconciled",
        },
        list_stdout=_TWO_MESSAGEABLE_AGENTS,
        message_sends=sends,
    )
    recipient = _integrated_router(runner, random.Random(0)).run_once()
    assert recipient == "agent-api"
    assert len(sends) == 1
    argv = sends[0]
    assert argv[:3] == ["mngr", "message", "agent-api"]
    body = argv[-1]
    assert "svc-web" in body
    assert "Exception: boom" in body
    # The own-window match must not leak into the alert (REQ-SCAN-2).
    assert _OWN_WINDOW not in body


def test_poll_batches_multiple_windows_into_one_message() -> None:
    sends: list[list[str]] = []
    runner = FakeCommandRunner(
        session="agent-session",
        windows=("svc-web", "svc-api"),
        pane_text_by_window={"svc-web": "Exception: boom", "svc-api": "ERROR: kaput"},
        list_stdout=_TWO_MESSAGEABLE_AGENTS,
        message_sends=sends,
    )
    _integrated_router(runner, random.Random(0)).run_once()
    assert len(sends) == 1
    body = sends[0][-1]
    assert "svc-web" in body
    assert "svc-api" in body


def test_poll_skips_when_no_messageable_agent() -> None:
    sends: list[list[str]] = []
    runner = FakeCommandRunner(
        session="agent-session",
        windows=("svc-web",),
        pane_text_by_window={"svc-web": "Exception: boom"},
        list_stdout=_ONLY_STOPPED_AGENT,
        message_sends=sends,
    )
    assert _integrated_router(runner, random.Random(0)).run_once() is None
    assert sends == []


def test_poll_never_messages_the_non_claude_system_agent() -> None:
    sends: list[list[str]] = []
    runner = FakeCommandRunner(
        session="agent-session",
        windows=("svc-web",),
        pane_text_by_window={"svc-web": "Exception: boom"},
        list_stdout=_CLAUDE_AND_MAIN_AGENTS,
        message_sends=sends,
    )
    assert _integrated_router(runner, random.Random(0)).run_once() == "agent-claude"
    assert [argv[2] for argv in sends] == ["agent-claude"]


def test_poll_tolerates_a_window_capture_failure() -> None:
    sends: list[list[str]] = []
    runner = FakeCommandRunner(
        session="agent-session",
        windows=("svc-broken", "svc-web"),
        pane_text_by_window={"svc-web": "Exception: boom"},
        list_stdout=_TWO_MESSAGEABLE_AGENTS,
        message_sends=sends,
        failing_windows=frozenset({"svc-broken"}),
    )
    assert _integrated_router(runner, random.Random(0)).run_once() == "agent-api"
    assert len(sends) == 1
    assert "svc-web" in sends[0][-1]
    assert "svc-broken" not in sends[0][-1]


def test_poll_ignores_errors_in_its_own_window() -> None:
    sends: list[list[str]] = []
    runner = FakeCommandRunner(
        session="agent-session",
        windows=(_OWN_WINDOW,),
        pane_text_by_window={_OWN_WINDOW: "Exception: boom in my own alert"},
        list_stdout=_TWO_MESSAGEABLE_AGENTS,
        message_sends=sends,
    )
    assert _integrated_router(runner, random.Random(0)).run_once() is None
    assert sends == []


def test_poll_returns_none_when_session_cannot_be_determined() -> None:
    sends: list[list[str]] = []
    runner = FakeCommandRunner(
        session="",
        windows=("svc-web",),
        pane_text_by_window={"svc-web": "Exception: boom"},
        list_stdout=_TWO_MESSAGEABLE_AGENTS,
        message_sends=sends,
    )
    assert _integrated_router(runner, random.Random(0)).run_once() is None
    assert sends == []
