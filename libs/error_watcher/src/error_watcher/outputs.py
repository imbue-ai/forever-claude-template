"""Error-output layer interface: the contract for delivering a batched alert.

`ErrorOutput` is the layer seam -- swap mngr for any other delivery channel.
This file holds only the contract: the `ErrorOutput` abstract base and the
`ErrorAlert` value type the routing layer hands it. The concrete mngr-backed
implementation lives in `mngr_agent_error_output.py`.
"""

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import NamedTuple


class ErrorAlert(NamedTuple):
    """The batched alert the routing layer hands to the output layer.

    `origin` is where the errors were seen (e.g. the tmux session name) and
    `matches_by_source` maps each source name to its newly-matched line(s). The
    output layer renders and delivers this however it sees fit.
    """

    origin: str
    matches_by_source: Mapping[str, Sequence[str]]


class ErrorOutput(ABC):
    """Error-output layer: delivers a batched alert to wherever it should go."""

    @abstractmethod
    def deliver(self, alert: ErrorAlert) -> str | None:
        """Deliver the alert, returning a delivery id (e.g. recipient) or None if undelivered."""
