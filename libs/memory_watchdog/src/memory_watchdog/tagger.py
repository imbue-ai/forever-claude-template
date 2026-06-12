from collections.abc import Sequence
from pathlib import Path
from typing import Final

from loguru import logger

from memory_watchdog.data_types import OOM_SCORE_ADJ_BY_TIER, ProcessClassification

_PROC_DIR: Final[Path] = Path("/proc")


def _oom_score_adj_path(pid: int) -> Path:
    return _PROC_DIR / str(pid) / "oom_score_adj"


def _read_current_adj(pid: int) -> int | None:
    try:
        return int(_oom_score_adj_path(pid).read_text().strip())
    except (OSError, ValueError):
        return None


def apply_oom_score_adjustments(
    classifications: Sequence[ProcessClassification],
) -> int:
    """Write each process's tier-derived oom_score_adj into /proc.

    Only writes when the current value differs, to avoid needless churn. Returns
    the number of processes whose adjustment was changed. Per-process failures
    (the process exited, or /proc is not writable for it) are skipped -- on
    gVisor the kernel ignores in-container oom_score_adj entirely, so failures
    here are expected and not worth more than a debug line.
    """
    changed_count = 0
    for classification in classifications:
        desired_adj = OOM_SCORE_ADJ_BY_TIER[classification.tier]
        current_adj = _read_current_adj(classification.pid)
        if current_adj == desired_adj:
            continue
        try:
            _oom_score_adj_path(classification.pid).write_text(f"{desired_adj}\n")
        except OSError as e:
            logger.debug(
                "Skipped oom_score_adj write for pid {} ({}): {}",
                classification.pid,
                classification.label,
                e,
            )
            continue
        changed_count = changed_count + 1
    return changed_count
