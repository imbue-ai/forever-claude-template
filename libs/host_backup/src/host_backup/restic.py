"""Thin wrappers around restic subprocess invocations.

Centralized so the tick loop can stay focused on orchestration. Every
function returns a CompletedProcess (never raises) and the caller decides
how to log + react. Stdout/stderr are always captured as text so they can
be embedded into jsonl events for forensic debugging.
"""

import json
import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Final

_RESTIC_TIMEOUT_SECONDS: Final[float] = 3600.0
_REPO_MISSING_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"unable to open config file", re.IGNORECASE),
    re.compile(r"repository does not exist", re.IGNORECASE),
    re.compile(r"does not appear to be a repository", re.IGNORECASE),
)


def is_repo_missing_error(stderr: str) -> bool:
    """Return True if `stderr` looks like a 'restic repo not initialized' error."""
    return any(p.search(stderr) for p in _REPO_MISSING_PATTERNS)


def run_restic(
    args: tuple[str, ...],
    *,
    env_overrides: Mapping[str, str],
    timeout_seconds: float = _RESTIC_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run `restic <args...>` with `env_overrides` merged onto `os.environ`."""
    env = dict(os.environ)
    env.update(env_overrides)
    return subprocess.run(
        ["restic", *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=timeout_seconds,
    )


def probe_repo(
    env_overrides: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    """`restic cat config` -- cheap probe; nonzero exit indicates missing repo or bad creds."""
    return run_restic(
        ("cat", "config"), env_overrides=env_overrides, timeout_seconds=60.0
    )


def init_repo(env_overrides: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
    """`restic init` -- create the repo on the remote backend."""
    return run_restic(("init",), env_overrides=env_overrides, timeout_seconds=120.0)


def backup(
    source_path: Path,
    excludes: tuple[str, ...],
    tag: str,
    env_overrides: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    """`restic backup --json <source> --tag <tag> [--exclude=<glob>...]`."""
    args: list[str] = ["backup", "--json", str(source_path), "--tag", tag]
    for pattern in excludes:
        args.append(f"--exclude={pattern}")
    return run_restic(tuple(args), env_overrides=env_overrides)


def forget(
    *,
    keep_hourly: int,
    keep_daily: int,
    keep_weekly: int,
    keep_monthly: int,
    env_overrides: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    """`restic forget --keep-* ...` (does not prune)."""
    args = (
        "forget",
        "--keep-hourly",
        str(keep_hourly),
        "--keep-daily",
        str(keep_daily),
        "--keep-weekly",
        str(keep_weekly),
        "--keep-monthly",
        str(keep_monthly),
    )
    return run_restic(args, env_overrides=env_overrides, timeout_seconds=600.0)


def prune(env_overrides: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
    """`restic prune` -- actually delete data referenced by no remaining snapshot."""
    return run_restic(("prune",), env_overrides=env_overrides)


def extract_snapshot_id_from_backup_output(stdout: str) -> str:
    """Pluck the snapshot_id from `restic backup --json` stdout (best-effort).

    Restic emits one JSON document per line; the final `summary` document
    carries the snapshot id. Returns "" when we can't find one.
    """
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("message_type") == "summary":
            sid = payload.get("snapshot_id")
            if isinstance(sid, str):
                return sid
    return ""
