"""On-disk layout of the shed ledger and the agent-pid registry -- the single
source of truth for where those files live.

The earlyoom kill hook (writer of the ledger), the agent-tagging SessionStart
hook (writer of the registry), the revival-notice SessionStart hook (reader of
the ledger), and the launch-task report poll (reader of the ledger) all resolve
their paths through this module, so the layout can't drift between producer and
consumers.

Everything lives under ``runtime/`` so it rides the runtime-backup branch and
survives container loss. The base resolves from ``OOM_PRIORITY_RUNTIME_DIR``
when set (pinned to an absolute path in ``.mngr/settings.toml`` so every agent's
hook and the container-level earlyoom hook agree regardless of their work dir),
otherwise relative to ``MNGR_AGENT_WORK_DIR`` (the repo root), falling back to
the current directory.

This module imports nothing beyond the standard library: the hooks that import
it run in a plain ``python3`` environment without this package's (nonexistent)
third-party dependencies, so they add ``src`` to ``sys.path`` and import it
directly.
"""

import os
from pathlib import Path
from typing import Final

_RUNTIME_DIR_ENV_VAR: Final[str] = "OOM_PRIORITY_RUNTIME_DIR"
_RUNTIME_SUBDIR: Final[Path] = Path("runtime") / "oom_priority"


def runtime_dir() -> Path:
    override = os.environ.get(_RUNTIME_DIR_ENV_VAR, "")
    if override:
        return Path(override)
    work_dir = os.environ.get("MNGR_AGENT_WORK_DIR", "")
    base = Path(work_dir) if work_dir else Path.cwd()
    return base / _RUNTIME_SUBDIR


def shed_ledger_path() -> Path:
    """Append-only record of every earlyoom kill plus revival-notice markers."""
    return runtime_dir() / "events" / "shed.jsonl"


def agent_pids_dir() -> Path:
    """Directory of one file per live agent, named by its main process's pid,
    recording which agent that pid is (so a killed pid maps back to its agent)."""
    return runtime_dir() / "agent_pids"
