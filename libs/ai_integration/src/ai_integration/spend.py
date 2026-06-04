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
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path

from imbue.imbue_common.mutable_model import MutableModel
from loguru import logger
from pydantic import Field

from ai_integration.errors import SpendCeilingExceededError

# Default rolling window: 24 hours.
DEFAULT_WINDOW_SECONDS = 86_400.0

# Where service definitions (and their optional spend config) live. Relative to
# the repo root, which is the cwd convention for services in this repo.
DEFAULT_SERVICES_TOML = Path("services.toml")

_Record = tuple[float, float]  # (timestamp, cost_usd)


def _default_escalate(message: str) -> None:
    logger.warning("ai_integration spend ceiling: {}", message)


class SpendTracker(MutableModel):
    """Tracks per-service spend against a rolling-window ceiling.

    A stateful Implementation: it owns the on-disk spend ledger under
    ``state_root/<service_name>/ai_spend.json``. ``clock`` and ``escalate`` are
    injected (tests pass deterministic fakes); ``escalate`` is called once when
    the ceiling is hit (default: log a warning -- a service should pass a
    callback that routes through ``send-user-message``).
    """

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    service_name: str = Field(description="The service whose spend this tracks")
    ceiling_usd: float = Field(
        description="Rolling-window spend ceiling; paused once cumulative spend meets it"
    )
    state_root: Path = Field(
        default=Path("runtime"),
        description="Root under which the per-service spend ledger is persisted",
    )
    window_seconds: float = Field(
        default=DEFAULT_WINDOW_SECONDS,
        description="Length of the rolling spend window in seconds",
    )
    clock: Callable[[], float] = Field(
        default=time.time, description="Wall-clock source (injected for tests)"
    )
    escalate: Callable[[str], None] = Field(
        default=_default_escalate,
        description="Called once when the ceiling is hit (default: log a warning)",
    )

    @property
    def _state_path(self) -> Path:
        return self.state_root / self.service_name / "ai_spend.json"

    def _load(self) -> list[_Record]:
        if not self._state_path.is_file():
            return []
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # The ledger is the budget guardrail; a corrupt/unreadable file is
            # tolerated (return empty rather than crash a service) but must be
            # logged so the silent spend reset it implies is observable.
            logger.warning(
                "ai_integration spend: could not read ledger {}; treating spend as "
                "empty (the rolling-window ceiling will not see prior spend): {}",
                self._state_path,
                exc,
            )
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
        cutoff = self.clock() - self.window_seconds
        return [(ts, cost) for ts, cost in records if ts >= cutoff]

    def spent_in_window(self) -> float:
        """Cumulative spend within the current rolling window."""
        return sum(cost for _ts, cost in self._within_window(self._load()))

    def record(self, cost_usd: float) -> None:
        """Append a paid call's cost and prune entries outside the window."""
        records = self._within_window(self._load())
        records.append((self.clock(), cost_usd))
        self._save(records)

    def check_ceiling(self) -> None:
        """Escalate and raise if cumulative spend has met/exceeded the ceiling.

        Call this *before* a paid call so the call is never made once the budget
        is exhausted.
        """
        spent = self.spent_in_window()
        if spent >= self.ceiling_usd:
            message = (
                f"service '{self.service_name}' has spent ~${spent:.2f} in the last "
                f"{self.window_seconds / 3600:.0f}h, at or over its ${self.ceiling_usd:.2f} "
                f"ceiling; pausing paid AI calls"
            )
            self.escalate(message)
            raise SpendCeilingExceededError(message)


def load_spend_tracker(
    service_name: str,
    *,
    services_toml_path: Path = DEFAULT_SERVICES_TOML,
    state_root: Path = Path("runtime"),
) -> SpendTracker | None:
    """Build a ``SpendTracker`` from a service's ``services.toml`` config, or ``None``.

    Spend tracking is **opt-in**: a service enables it by adding an
    ``[services.<name>.ai_spend]`` table with at least ``ceiling_usd`` (and an
    optional ``window_seconds``, defaulting to 24h). Returns ``None`` when the
    file, the service entry, or the ``ai_spend`` table is absent -- so an
    unconfigured service simply runs without a ceiling.

    The ``ai_spend`` table is independent of ``command`` / ``restart``: a service
    that needs spend tracking but is *not* a continuously-running background
    process (e.g. one invoked on demand) can declare ``[services.<name>.ai_spend]``
    with no ``command`` at all. The bootstrap manager skips command-less entries,
    so nothing is launched, while this loader still finds the spend config by name.
    """
    if not services_toml_path.is_file():
        return None
    try:
        data = tomllib.loads(services_toml_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(
            "ai_integration spend: could not read {}; spend tracking disabled: {}",
            services_toml_path,
            exc,
        )
        return None
    services = data.get("services")
    if not isinstance(services, dict):
        return None
    service = services.get(service_name)
    if not isinstance(service, dict):
        return None
    ai_spend = service.get("ai_spend")
    if not isinstance(ai_spend, dict):
        return None
    ceiling = ai_spend.get("ceiling_usd")
    # ``bool`` is an ``int`` subclass; exclude it so ``ceiling_usd = true`` is not
    # silently treated as a $1 ceiling.
    if isinstance(ceiling, bool) or not isinstance(ceiling, (int, float)):
        logger.warning(
            "ai_integration spend: [services.{}.ai_spend] has no numeric ceiling_usd; "
            "spend tracking disabled for this service",
            service_name,
        )
        return None
    window = ai_spend.get("window_seconds", DEFAULT_WINDOW_SECONDS)
    if isinstance(window, bool) or not isinstance(window, (int, float)):
        window = DEFAULT_WINDOW_SECONDS
    return SpendTracker(
        service_name=service_name,
        ceiling_usd=float(ceiling),
        state_root=state_root,
        window_seconds=float(window),
    )
