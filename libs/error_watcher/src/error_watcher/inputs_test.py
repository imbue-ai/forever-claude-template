"""Unit tests for the tmux error-input layer."""

from error_watcher.inputs import ErrorSource, TmuxWindowErrorInput
from error_watcher.testing import FakeCommandRunner

_OWN_WINDOW = "svc-error-watcher"


def _make_input(runner: FakeCommandRunner) -> TmuxWindowErrorInput:
    return TmuxWindowErrorInput(runner, _OWN_WINDOW)


def test_read_returns_origin_and_each_windows_content() -> None:
    runner = FakeCommandRunner(
        session="agent-session",
        windows=("svc-web", "svc-api"),
        pane_text_by_window={"svc-web": "Exception: boom", "svc-api": "all good"},
        list_stdout="",
        message_sends=[],
    )
    reading = _make_input(runner).read()
    assert reading.origin == "agent-session"
    assert reading.sources == (
        ErrorSource(name="svc-web", content="Exception: boom"),
        ErrorSource(name="svc-api", content="all good"),
    )


def test_read_excludes_its_own_window() -> None:
    # The watcher's own window must never be read, so its alert text (which
    # contains "error") cannot re-trigger a match (REQ-SCAN-2).
    runner = FakeCommandRunner(
        session="agent-session",
        windows=("svc-web", _OWN_WINDOW),
        pane_text_by_window={
            "svc-web": "all good",
            _OWN_WINDOW: "Possible error/exception detected ...",
        },
        list_stdout="",
        message_sends=[],
    )
    reading = _make_input(runner).read()
    assert [source.name for source in reading.sources] == ["svc-web"]


def test_read_returns_empty_origin_when_session_cannot_be_determined() -> None:
    runner = FakeCommandRunner(
        session="",
        windows=("svc-web",),
        pane_text_by_window={"svc-web": "Exception: boom"},
        list_stdout="",
        message_sends=[],
    )
    reading = _make_input(runner).read()
    assert reading.origin == ""
    assert reading.sources == ()


def test_read_keeps_a_window_that_fails_to_capture_with_empty_content() -> None:
    # A window that vanished between enumeration and capture is tolerated: it is
    # reported with empty content rather than raising (REQ-SCAN-3), so its dedup
    # state is only pruned once it actually leaves the window list.
    runner = FakeCommandRunner(
        session="agent-session",
        windows=("svc-broken", "svc-web"),
        pane_text_by_window={"svc-web": "Exception: boom"},
        list_stdout="",
        message_sends=[],
        failing_windows=frozenset({"svc-broken"}),
    )
    reading = _make_input(runner).read()
    assert reading.sources == (
        ErrorSource(name="svc-broken", content=""),
        ErrorSource(name="svc-web", content="Exception: boom"),
    )
