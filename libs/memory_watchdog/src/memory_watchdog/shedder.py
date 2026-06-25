import os
import signal
from collections import defaultdict
from collections.abc import Sequence
from typing import Final

from imbue.imbue_common.pure import pure
from loguru import logger

from memory_watchdog.data_types import (
    SHEDDABLE_TIERS_IN_SHED_ORDER,
    TIER_RANK_BY_TIER,
    ProcessClassification,
    RecentShedSummary,
    ShedRecord,
    Tier,
    now_iso_timestamp,
)

# Shed-priority index per tier: AGENT_CHILD first (most expendable), USER_AGENT
# last. Used to order shed candidates across tiers while preserving the tier
# hierarchy -- a more-protected process is never shed before a less-protected
# one, regardless of resident size.
_SHED_ORDER_INDEX: Final[dict[Tier, int]] = {
    tier: index for index, tier in enumerate(SHEDDABLE_TIERS_IN_SHED_ORDER)
}


@pure
def _projected_used_fraction(available_kb: int, freed_kb: int, total_kb: int) -> float:
    """Used fraction we expect once `freed_kb` of resident memory is reclaimed."""
    if total_kb <= 0:
        return 0.0
    return 1.0 - ((available_kb + freed_kb) / total_kb)


@pure
def select_processes_to_shed(
    classifications: Sequence[ProcessClassification],
    available_kb: int,
    total_kb: int,
    relief_threshold: float,
    min_resident_kb: int,
) -> list[ProcessClassification]:
    """Choose the individual processes to shed, stopping as soon as the projected
    post-shed usage drops below the relief threshold.

    Candidates are ordered by tier shed-priority first (AGENT_CHILD before
    WORKER_AGENT before AUXILIARY_SERVICE before USER_AGENT) and, within a tier,
    largest resident set first. So the cheapest, biggest wins come first and we
    stop the instant the projection clears relief -- shedding the one process
    actually holding the memory rather than its whole tier. This is what keeps a
    single large agent-child hog from taking down the agent's claude, its
    transcript streamer, its lead's report poll, and every other tier-8 helper
    alongside it.

    Processes whose resident set is below `min_resident_kb` are never shed:
    killing them frees too little to move the needle, so doing so would be pure
    collateral (sleeps, transcript streamers, coordination polls). The next poll
    re-reads real usage and sheds again if the estimate fell short.

    The projection is based on the processes' resident memory -- what shedding
    them is expected to reclaim -- rather than re-reading /proc/meminfo between
    kills. The kernel reclaims a SIGKILLed process's pages asynchronously, so an
    immediate re-read still reports the pre-kill usage and would make the shedder
    over-shed.
    """
    candidates = [
        classification
        for classification in classifications
        if classification.tier in _SHED_ORDER_INDEX
        and classification.resident_kb >= min_resident_kb
    ]
    candidates.sort(key=lambda c: (_SHED_ORDER_INDEX[c.tier], -c.resident_kb))
    chosen: list[ProcessClassification] = []
    freed_kb = 0
    for candidate in candidates:
        if (
            _projected_used_fraction(available_kb, freed_kb, total_kb)
            < relief_threshold
        ):
            break
        chosen.append(candidate)
        freed_kb += candidate.resident_kb
    return chosen


@pure
def summarize_recent_sheds(records: Sequence[ShedRecord]) -> list[RecentShedSummary]:
    """Aggregate shed records for the UI banner.

    Grouped by (label, owning agent) so the same command run by two different
    agents stays on separate lines and each line can name its owning agent.
    """
    count_by_key: dict[tuple[str, str | None], int] = defaultdict(int)
    reclaimed_by_key: dict[tuple[str, str | None], int] = defaultdict(int)
    rank_by_key: dict[tuple[str, str | None], int] = {}
    for record in records:
        key = (record.label, record.owning_agent_name)
        count_by_key[key] = count_by_key[key] + 1
        reclaimed_by_key[key] = reclaimed_by_key[key] + record.resident_kb
        rank_by_key[key] = record.tier_rank
    summaries = [
        RecentShedSummary(
            label=label,
            tier_rank=rank_by_key[(label, owning_agent_name)],
            count=count,
            reclaimed_kb=reclaimed_by_key[(label, owning_agent_name)],
            owning_agent_name=owning_agent_name,
        )
        for (label, owning_agent_name), count in count_by_key.items()
    ]
    return sorted(summaries, key=lambda s: s.reclaimed_kb, reverse=True)


def _kill_process(pid: int) -> bool:
    """SIGKILL one process. Returns whether the signal was delivered.

    SIGKILL (not SIGTERM) is deliberate: under memory pressure we need the
    resident set reclaimed immediately, and waiting for graceful shutdown is a
    luxury we do not have. A vanished process (already dead) is treated as a
    success -- the goal state is reached either way.
    """
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except PermissionError as e:
        logger.warning("Not permitted to kill pid {}: {}", pid, e)
        return False
    return True


def shed_processes(targets: Sequence[ProcessClassification]) -> list[ShedRecord]:
    """SIGKILL each chosen process and return a ledger record per kill.

    The watchdog's own process and its process group are never selected (they
    are not in a sheddable tier), but are skipped defensively.
    """
    own_pid = os.getpid()
    own_group = os.getpgrp()
    records: list[ShedRecord] = []
    for target in targets:
        if target.pid == own_pid:
            continue
        try:
            if os.getpgid(target.pid) == own_group:
                continue
        except (ProcessLookupError, PermissionError):
            pass
        if not _kill_process(target.pid):
            continue
        is_agent_process = target.tier in (Tier.USER_AGENT, Tier.WORKER_AGENT)
        records.append(
            ShedRecord(
                timestamp=now_iso_timestamp(),
                tier=target.tier,
                tier_rank=TIER_RANK_BY_TIER[target.tier],
                label=target.label,
                pid=target.pid,
                resident_kb=target.resident_kb,
                agent_name=target.label if is_agent_process else None,
                owning_agent_name=target.owning_agent_name,
            )
        )
    return records
