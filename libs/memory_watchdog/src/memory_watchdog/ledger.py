import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from loguru import logger

from memory_watchdog.data_types import MemoryStatus, ShedRecord, now_iso_timestamp

# Both files live under runtime/ so they ride the runtime-backup branch and
# survive container loss. The ledger is the append-only history; the status file
# is the current-state read API for the UI banner and for pressure checks.
#
# These path helpers are the single source of truth for the layout: the watchdog
# (writer), the system interface (status reader), and bootstrap (block/unblock
# writer) all import them, so the schema location can't drift between producer
# and consumers. The base resolves relative to the agent work dir (the repo
# root, where every service runs), falling back to the current directory, and is
# overridable in full via MEMORY_WATCHDOG_RUNTIME_DIR -- used by tests, and
# honored uniformly so a production override can't make readers and the writer
# diverge.
_RUNTIME_DIR_ENV_VAR: Final[str] = "MEMORY_WATCHDOG_RUNTIME_DIR"
_RUNTIME_SUBDIR: Final[Path] = Path("runtime") / "memory_watchdog"


def _watchdog_runtime_dir() -> Path:
    override = os.environ.get(_RUNTIME_DIR_ENV_VAR, "")
    if override:
        return Path(override)
    work_dir = os.environ.get("MNGR_AGENT_WORK_DIR", "")
    base = Path(work_dir) if work_dir else Path.cwd()
    return base / _RUNTIME_SUBDIR


def shed_ledger_path() -> Path:
    return _watchdog_runtime_dir() / "events" / "shed" / "events.jsonl"


def status_path() -> Path:
    return _watchdog_runtime_dir() / "status.json"


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
            }
        )


def record_service_blocked(service_name: str, reason: str) -> None:
    """Record that bootstrap paused a crash-looping service under pressure."""
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
