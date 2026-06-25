import json
import os
import tempfile
from collections.abc import Sequence
from typing import Final

from loguru import logger

from memory_watchdog.data_types import MemoryStatus, ShedRecord, now_iso_timestamp

# The ledger is the append-only history; the status file is the current-state
# read API for the UI banner and for pressure checks. Their on-disk locations
# come from memory_watchdog.paths -- the single dependency-free source of truth
# shared with the system interface and the revival hook (re-exported here so
# existing ``from memory_watchdog.ledger import ...`` callers keep working).
from memory_watchdog.paths import shed_ledger_path, status_path

# Record-type tags written into the ledger's "type" field.
_RECORD_TYPE_PROCESS_SHED: Final[str] = "process_shed"
_RECORD_TYPE_SERVICE_BLOCKED: Final[str] = "service_blocked"
_RECORD_TYPE_SERVICE_UNBLOCKED: Final[str] = "service_unblocked"
_RECORD_TYPE_NOTICE_DELIVERED: Final[str] = "notice_delivered"


def _append_ledger_line(record: dict[str, object]) -> None:
    ledger_path = shed_ledger_path()
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger_path, "a") as ledger_file:
        ledger_file.write(json.dumps(record) + "\n")


def append_shed_records(records: Sequence[ShedRecord]) -> None:
    """Append one ledger line per shed process."""
    for record in records:
        _append_ledger_line(
            {
                "timestamp": record.timestamp,
                "type": _RECORD_TYPE_PROCESS_SHED,
                "tier": str(record.tier),
                "tier_rank": record.tier_rank,
                "label": record.label,
                "pid": record.pid,
                "resident_kb": record.resident_kb,
                "agent_name": record.agent_name,
                "owning_agent_name": record.owning_agent_name,
            }
        )


def record_service_blocked(service_name: str, reason: str) -> None:
    """Record that a crash-looping service was paused under pressure.

    Reserved: no caller writes these today (supervisord now owns restarts). Kept
    for a future supervisorctl-driven poller -- see README's crash-loop section.
    """
    _append_ledger_line(
        {
            "timestamp": now_iso_timestamp(),
            "type": _RECORD_TYPE_SERVICE_BLOCKED,
            "service": service_name,
            "reason": reason,
        }
    )


def record_service_unblocked(service_name: str) -> None:
    """Record that a previously paused service resumed."""
    _append_ledger_line(
        {
            "timestamp": now_iso_timestamp(),
            "type": _RECORD_TYPE_SERVICE_UNBLOCKED,
            "service": service_name,
        }
    )


def read_currently_blocked_services() -> list[str]:
    """Compute which services are presently paused, from the append-only ledger.

    A ``service_blocked`` line marks a service paused; a later
    ``service_unblocked`` line for the same service clears it. Returns the
    services currently in the blocked state, sorted.
    """
    ledger_path = shed_ledger_path()
    if not ledger_path.exists():
        return []
    blocked: set[str] = set()
    try:
        ledger_text = ledger_path.read_text()
    except OSError as e:
        logger.warning("Failed to read shed ledger for blocked services: {}", e)
        return []
    for line in ledger_text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        record_type = record.get("type")
        service = record.get("service")
        if not isinstance(service, str):
            continue
        if record_type == _RECORD_TYPE_SERVICE_BLOCKED:
            blocked.add(service)
        elif record_type == _RECORD_TYPE_SERVICE_UNBLOCKED:
            blocked.discard(service)
    return sorted(blocked)


def write_status(status: MemoryStatus) -> None:
    """Atomically write the current status file (the UI banner's data source)."""
    target_path = status_path()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    payload = status.model_dump_json()
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=target_path.parent, prefix="status.", suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w") as tmp_file:
            tmp_file.write(payload)
        os.replace(tmp_name, target_path)
    except OSError as e:
        logger.warning("Failed to write watchdog status file: {}", e)
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
