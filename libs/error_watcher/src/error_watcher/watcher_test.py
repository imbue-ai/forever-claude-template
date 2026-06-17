"""Unit tests for the watcher service entry point."""

import signal

import pytest

from error_watcher.watcher import _handle_signal


def test_handle_signal_exits_cleanly() -> None:
    # The stop-signal handler must exit 0 so the poll loop terminates cleanly
    # when the bootstrap manager stops the service (REQ-SPAWN-2). SIGHUP is the
    # signal `tmux kill-window` actually delivers.
    with pytest.raises(SystemExit) as exc_info:
        _handle_signal(signal.SIGHUP, None)
    assert exc_info.value.code == 0
