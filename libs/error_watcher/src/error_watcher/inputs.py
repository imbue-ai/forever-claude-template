"""Error-input layer interface: the contract every error source must satisfy.

The routing layer consumes a source-agnostic `ErrorReading`, so the way errors
are observed can be swapped without the routing or output layers changing. This
file holds only the contract -- the `ErrorInput` abstract base and the
`ErrorReading` / `ErrorSource` value types the layers communicate through. The
concrete tmux implementation lives in `tmux_window_error_input.py`; a future
input (e.g. one that reads systemd/journald units) is a drop-in sibling that
returns the same `ErrorReading`.
"""

from abc import ABC, abstractmethod
from typing import NamedTuple


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
