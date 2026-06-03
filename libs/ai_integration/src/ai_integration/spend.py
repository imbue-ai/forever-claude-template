"""Per-service spend tracking with a rolling-window ceiling.

A service records the estimated/actual USD cost of each paid call. Before making
a call the service checks the ceiling; once cumulative spend in the rolling
window meets or exceeds it, ``check_ceiling`` escalates to the user (via an
injected callback) and raises ``SpendCeilingExceededError`` rather than letting
volume silently run past the budget.

State persists as JSON under ``runtime/<service>/`` so the window survives
service restarts.
"""

import json
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from loguru import logger

from ai_integration.errors import SpendCeilingExceededError

# Default rolling window: 24 hours.
DEFAULT_WINDOW_SECONDS = 86_400.0

_Record = tuple[float, float]  # (timestamp, cost_usd)


def _default_escalate(message: str) -> None:
    logger.warning("ai_integration spend ceiling: {}", message)


class SpendTracker:
    """Tracks per-service spend against a rolling-window ceiling.

    ``clock`` and ``escalate`` are injectable for testing. ``escalate`` is called
    once when the ceiling is hit (default: log a warning -- a service should pass
    a callback that routes through ``send-user-message``).
    """

    def __init__(
        self,
        service_name: str,
        ceiling_usd: float,
        state_root: Path = Path("runtime"),
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        clock: Callable[[], float] = time.time,
        escalate: Callable[[str], None] = _default_escalate,
    ) -> None:
        self._service_name = service_name
        self._ceiling_usd = ceiling_usd
        self._state_path = state_root / service_name / "ai_spend.json"
        self._window_seconds = window_seconds
        self._clock = clock
        self._escalate = escalate

    def _load(self) -> list[_Record]:
        if not self._state_path.is_file():
            return []
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(raw, list):
            return []
        records: list[_Record] = []
        for entry in raw:
            if (
                isinstance(entry, Sequence)
                and not isinstance(entry, str)
                and len(entry) == 2
            ):
                ts, cost = entry
                if isinstance(ts, (int, float)) and isinstance(cost, (int, float)):
                    records.append((float(ts), float(cost)))
        return records

    def _save(self, records: Sequence[_Record]) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(
            json.dumps([list(r) for r in records]), encoding="utf-8"
        )

    def _within_window(self, records: Sequence[_Record]) -> list[_Record]:
        cutoff = self._clock() - self._window_seconds
        return [(ts, cost) for ts, cost in records if ts >= cutoff]

    def spent_in_window(self) -> float:
        """Cumulative spend within the current rolling window."""
        return sum(cost for _ts, cost in self._within_window(self._load()))

    def record(self, cost_usd: float) -> None:
        """Append a paid call's cost and prune entries outside the window."""
        records = self._within_window(self._load())
        records.append((self._clock(), cost_usd))
        self._save(records)

    def check_ceiling(self) -> None:
        """Escalate and raise if cumulative spend has met/exceeded the ceiling.

        Call this *before* a paid call so the call is never made once the budget
        is exhausted.
        """
        spent = self.spent_in_window()
        if spent >= self._ceiling_usd:
            message = (
                f"service '{self._service_name}' has spent ~${spent:.2f} in the last "
                f"{self._window_seconds / 3600:.0f}h, at or over its ${self._ceiling_usd:.2f} "
                f"ceiling; pausing paid AI calls"
            )
            self._escalate(message)
            raise SpendCeilingExceededError(message)
