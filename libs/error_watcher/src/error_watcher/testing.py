"""Test helpers for the error-watcher layers (imported by the _test.py modules).

`FakeCommandRunner` drives the real tmux input and mngr output layers without
touching a real tmux/mngr, so a full poll can be exercised end to end.
`FakeErrorInput` / `RecordingErrorOutput` stand in for the layer interfaces so
the routing core can be tested in isolation from any I/O.
"""

from collections.abc import Mapping, Sequence
from typing import NamedTuple

from error_watcher.commands import CommandResult
from error_watcher.inputs import ErrorInput, ErrorReading
from error_watcher.outputs import ErrorAlert, ErrorOutput


class FakeCommandRunner(NamedTuple):
    """Maps each tmux/mngr argv to a canned result so a full poll runs without real tmux/mngr.

    Records every `mngr message` argv in `message_sends` so a test can assert
    exactly one batched alert was sent and to whom. Windows named in
    `failing_windows` return a non-zero capture, simulating a window that
    vanished mid-poll; recipients in `failing_recipients` (or all of them when
    `send_fails`) return a non-zero send.
    """

    session: str
    windows: tuple[str, ...]
    pane_text_by_window: Mapping[str, str]
    list_stdout: str
    message_sends: list[list[str]]
    failing_windows: frozenset[str] = frozenset()
    send_fails: bool = False
    failing_recipients: frozenset[str] = frozenset()
    list_returncode: int = 0

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
            return CommandResult(self.list_returncode, self.list_stdout, "")
        if argv[:2] == ["mngr", "message"]:
            self.message_sends.append(argv)
            recipient = argv[2]
            if self.send_fails or recipient in self.failing_recipients:
                return CommandResult(1, "", "delivery failed")
            return CommandResult(0, "", "")
        return CommandResult(127, "", f"unexpected command: {argv}")


class SequencedErrorInput(ErrorInput):
    """Returns a scripted sequence of ErrorReadings on successive reads (repeating the last).

    Lets a router test vary the world across polls -- e.g. a window closing
    between reads -- without a real input. A single-element sequence is a fixed
    reading.
    """

    def __init__(self, readings: Sequence[ErrorReading]) -> None:
        self._readings = list(readings)
        self._index = 0

    def read(self) -> ErrorReading:
        reading = self._readings[min(self._index, len(self._readings) - 1)]
        self._index += 1
        return reading


class RecordingErrorOutput(ErrorOutput):
    """Records every alert it is asked to deliver and returns scripted delivery results.

    `results` is consumed one per deliver() call; the last value repeats once
    exhausted, so a single-element sequence is a constant result.
    """

    def __init__(self, results: Sequence[str | None]) -> None:
        self._results = list(results)
        self._index = 0
        self.delivered: list[ErrorAlert] = []

    def deliver(self, alert: ErrorAlert) -> str | None:
        self.delivered.append(alert)
        result = self._results[min(self._index, len(self._results) - 1)]
        self._index += 1
        return result
