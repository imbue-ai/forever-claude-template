"""The shed ledger: an append-only JSON-lines record of earlyoom kills and the
revival notices delivered for them.

Two record types share the file:

- ``process_shed`` -- one per earlyoom kill, written by the kill hook. Carries
  ``agent_name`` only when an agent's *own* main process was shed (looked up in
  the pid registry); that is what marks an agent as needing a revival notice. A
  shed subprocess has ``agent_name`` null.
- ``notice_delivered`` -- written by the revival-notice hook when it has told a
  revived agent it was paused, recording the latest shed timestamp covered so
  the same notice is not injected twice.

"Pending" for an agent means: a ``process_shed`` for that agent newer than the
latest ``notice_delivered`` for it. The revival hook uses this to decide whether
to inject a notice; the launch-task report poll uses it to detect that a worker
was paused and will not report until revived.

Stdlib-only (see ``paths``): imported by Claude hooks and the launch-task script
under a plain ``python3``.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from oom_priority.paths import shed_ledger_path

RECORD_TYPE_PROCESS_SHED: Final[str] = "process_shed"
RECORD_TYPE_NOTICE_DELIVERED: Final[str] = "notice_delivered"


def now_timestamp() -> str:
    """Current UTC time as a sortable ISO 8601 string (microsecond precision)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _append(record: dict) -> None:
    path = shed_ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as ledger_file:
        ledger_file.write(json.dumps(record) + "\n")


def append_shed_record(
    pid: int,
    comm: str,
    agent_name: str | None,
    is_worker: bool | None,
) -> None:
    """Append one ``process_shed`` line for an earlyoom kill.

    ``agent_name``/``is_worker`` are set only when the killed pid was an agent's
    own main process (per the registry); for a shed subprocess they are None.
    """
    _append(
        {
            "timestamp": now_timestamp(),
            "type": RECORD_TYPE_PROCESS_SHED,
            "pid": pid,
            "comm": comm,
            "agent_name": agent_name,
            "is_worker": is_worker,
        }
    )


def append_notice_delivered(agent_name: str, up_to_timestamp: str) -> None:
    """Mark that ``agent_name`` has been told about shed events through
    ``up_to_timestamp``, so the notice is not repeated."""
    _append(
        {
            "timestamp": now_timestamp(),
            "type": RECORD_TYPE_NOTICE_DELIVERED,
            "agent_name": agent_name,
            "up_to_timestamp": up_to_timestamp,
        }
    )


def read_records(path: Path | None = None) -> list[dict]:
    """Parse the ledger into a list of records (empty if it does not exist).

    Malformed lines are skipped rather than raising, so a partially-written line
    (the writer appends without locking) never breaks a reader.
    """
    ledger_path = path if path is not None else shed_ledger_path()
    if not ledger_path.exists():
        return []
    records: list[dict] = []
    try:
        text = ledger_path.read_text()
    except OSError:
        return []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def latest_delivered_timestamp(records: list[dict], agent_name: str) -> str:
    """Highest ``up_to_timestamp`` already delivered to ``agent_name`` (or "")."""
    delivered = [
        str(record.get("up_to_timestamp", ""))
        for record in records
        if record.get("type") == RECORD_TYPE_NOTICE_DELIVERED
        and record.get("agent_name") == agent_name
    ]
    return max(delivered) if delivered else ""


def pending_shed_timestamps(records: list[dict], agent_name: str) -> list[str]:
    """Timestamps of ``agent_name``'s own shed records not yet covered by a
    delivered notice."""
    after = latest_delivered_timestamp(records, agent_name)
    pending: list[str] = []
    for record in records:
        if record.get("type") != RECORD_TYPE_PROCESS_SHED:
            continue
        if record.get("agent_name") != agent_name:
            continue
        timestamp = str(record.get("timestamp", ""))
        if timestamp and timestamp > after:
            pending.append(timestamp)
    return pending


def has_pending_shed(agent_name: str) -> bool:
    """Whether ``agent_name``'s main process was shed and not yet revived."""
    return len(pending_shed_timestamps(read_records(), agent_name)) > 0
