"""Error-input layer: reads the current content of every watchable source.

The routing layer consumes a source-agnostic `ErrorReading`, so the way errors
are observed can be swapped without the routing or output layers changing.
`TmuxWindowErrorInput` is the only concrete input today -- it wraps the existing
tmux capture work behind the `ErrorInput` contract -- but a future input (e.g.
one that reads systemd/journald units) is a drop-in sibling that returns the
same `ErrorReading`.
"""

from abc import ABC, abstractmethod
from typing import NamedTuple

from loguru import logger

from error_watcher.commands import CommandRunner


class ErrorSource(NamedTuple):
    """One watchable source and its current rendered content."""

    name: str
    content: str


class ErrorReading(NamedTuple):
    """A single read of every watchable source.

    `origin` identifies where the sources live (e.g. the tmux session name) and
    is empty when the input could not determine its origin -- the router treats
    an empty origin as "nothing to scan this poll". `sources` already excludes
    the watcher's own source, so its alert text cannot re-trigger a match.
    """

    origin: str
    sources: tuple[ErrorSource, ...]


class ErrorInput(ABC):
    """Error-input layer: enumerates watchable sources and their current content."""

    @abstractmethod
    def read(self) -> ErrorReading:
        """Read every watchable source once, returning its origin and per-source content."""


def get_session_name(run: CommandRunner) -> str:
    """Return the watcher's own tmux session name, or "" if it cannot be determined."""
    result = run(["tmux", "display-message", "-p", "#S"])
    if result.returncode != 0:
        logger.warning(
            "Could not determine tmux session name: {}", result.stderr.strip()
        )
        return ""
    return result.stdout.strip()


def list_windows(run: CommandRunner, session: str) -> list[str]:
    """Return every window name in `session` (empty list on failure, REQ-SCAN-1)."""
    result = run(["tmux", "list-windows", "-t", session, "-F", "#{window_name}"])
    if result.returncode != 0:
        logger.warning(
            "Could not list windows for session {}: {}", session, result.stderr.strip()
        )
        return []
    return [line for line in result.stdout.splitlines() if line]


def capture_window(run: CommandRunner, session: str, window: str) -> str:
    """Return the visible pane text of `window` ("" if it could not be captured).

    A window can be destroyed between enumeration and capture; that is tolerated
    by returning empty text rather than raising (REQ-SCAN-3).
    """
    result = run(["tmux", "capture-pane", "-t", f"{session}:{window}", "-p"])
    if result.returncode != 0:
        logger.debug(
            "Could not capture window {} (it may have closed): {}",
            window,
            result.stderr.strip(),
        )
        return ""
    return result.stdout


class TmuxWindowErrorInput(ErrorInput):
    """Reads every tmux window in the watcher's own session, except its own window."""

    def __init__(self, run: CommandRunner, own_window: str) -> None:
        self._run = run
        # The watcher's own service window, excluded so its alert text (which
        # contains "error") cannot re-trigger a match (REQ-SCAN-2).
        self._own_window = own_window

    def read(self) -> ErrorReading:
        session = get_session_name(self._run)
        if not session:
            return ErrorReading(origin="", sources=())
        sources: list[ErrorSource] = []
        for window in list_windows(self._run, session):
            if window == self._own_window:
                continue
            # A window that vanished mid-poll captures as "" rather than raising;
            # it is still reported as a (content-less) source so its dedup state
            # is not pruned until it actually disappears from the window list.
            content = capture_window(self._run, session, window)
            sources.append(ErrorSource(name=window, content=content))
        return ErrorReading(origin=session, sources=tuple(sources))
