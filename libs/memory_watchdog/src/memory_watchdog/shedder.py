import os
import signal
from collections import defaultdict
from collections.abc import Sequence

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


@pure
def _projected_used_fraction(available_kb: int, freed_kb: int, total_kb: int) -> float:
    """Used fraction we expect once `freed_kb` of resident memory is reclaimed."""
    if total_kb <= 0:
        return 0.0
    return 1.0 - ((available_kb + freed_kb) / total_kb)


@pure
def select_tiers_to_shed(
    classifications: Sequence[ProcessClassification],
    available_kb: int,
    total_kb: int,
    relief_threshold: float,
) -> list[Tier]:
    """Choose which tiers to shed, most-expendable first, stopping as soon as the
    *projected* post-shed usage drops below the relief threshold.

    The projection is based on the resident memory of the processes in each tier
    -- what shedding them is expected to reclaim -- rather than re-reading
    /proc/meminfo between kills. The kernel reclaims a SIGKILLed process's pages
    asynchronously, so an immediate re-read still reports the pre-kill usage and
    would make the shedder escalate into the protected tiers (e.g. kill a user's
    agent) even though the cheap tier it just shed already freed enough. The next
    poll re-reads real usage and sheds more if the estimate fell short.
    """
    resident_by_tier: dict[Tier, int] = defaultdict(int)
    count_by_tier: dict[Tier, int] = defaultdict(int)
    for classification in classifications:
        resident_by_tier[classification.tier] += classification.resident_kb
        count_by_tier[classification.tier] += 1
    chosen: list[Tier] = []
    freed_kb = 0
    for tier in SHEDDABLE_TIERS_IN_SHED_ORDER:
        if _projected_used_fraction(available_kb, freed_kb, total_kb) < relief_threshold:
            break
        if count_by_tier.get(tier, 0) == 0:
            continue
        chosen.append(tier)
        freed_kb += resident_by_tier.get(tier, 0)
    return chosen


@pure
def select_shed_targets(
    classifications: Sequence[ProcessClassification],
    tier: Tier,
) -> list[ProcessClassification]:
    """Pick every process in the given tier, largest resident set first.

    Largest-first ordering only affects the ledger and the order of kills within
    the tier; the whole tier is shed regardless.
    """
    in_tier = [c for c in classifications if c.tier == tier]
    return sorted(in_tier, key=lambda c: c.resident_kb, reverse=True)


@pure
def summarize_recent_sheds(records: Sequence[ShedRecord]) -> list[RecentShedSummary]:
    """Aggregate shed records by label for the UI banner."""
    count_by_label: dict[str, int] = defaultdict(int)
    reclaimed_by_label: dict[str, int] = defaultdict(int)
    rank_by_label: dict[str, int] = {}
    for record in records:
        count_by_label[record.label] = count_by_label[record.label] + 1
        reclaimed_by_label[record.label] = (
            reclaimed_by_label[record.label] + record.resident_kb
        )
        rank_by_label[record.label] = record.tier_rank
    summaries = [
        RecentShedSummary(
            label=label,
            tier_rank=rank_by_label[label],
            count=count,
            reclaimed_kb=reclaimed_by_label[label],
        )
        for label, count in count_by_label.items()
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


def shed_tier(
    classifications: Sequence[ProcessClassification],
    tier: Tier,
) -> list[ShedRecord]:
    """Kill every process in the tier and return a ledger record per kill.

    The watchdog's own process and its process group are never in a sheddable
    tier, but are skipped defensively.
    """
    own_pid = os.getpid()
    own_group = os.getpgrp()
    targets = select_shed_targets(classifications, tier)
    tier_rank = TIER_RANK_BY_TIER[tier]
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
        is_agent_process = tier in (Tier.USER_AGENT, Tier.WORKER_AGENT)
        records.append(
            ShedRecord(
                timestamp=now_iso_timestamp(),
                tier=tier,
                tier_rank=tier_rank,
                label=target.label,
                pid=target.pid,
                resident_kb=target.resident_kb,
                agent_name=target.label if is_agent_process else None,
            )
        )
    return records
