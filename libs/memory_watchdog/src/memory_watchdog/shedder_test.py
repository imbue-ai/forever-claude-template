import subprocess

from memory_watchdog.data_types import ProcessClassification, ShedRecord, Tier
from memory_watchdog.shedder import (
    select_shed_targets,
    select_tiers_to_shed,
    shed_tier,
    summarize_recent_sheds,
)

# Relief at 80% used: the watchdog stops escalating once projected usage drops
# below this. Matches watchdog.SHED_RELIEF_THRESHOLD.
_RELIEF = 0.80


def _classification(
    pid: int, tier: Tier, resident_kb: int, label: str
) -> ProcessClassification:
    return ProcessClassification(
        pid=pid, resident_kb=resident_kb, tier=tier, label=label
    )


def test_select_tiers_stops_at_first_tier_when_it_frees_enough() -> None:
    # A 800MB agent-child hog under acute pressure (50MB available of 1000MB).
    # Shedding tier 8 alone projects usage down to 15%, so the user's agent
    # (tier 5) must NOT be selected -- this is the over-shed regression guard.
    classifications = [
        _classification(1, Tier.AGENT_CHILD, 800_000, "hog"),
        _classification(2, Tier.USER_AGENT, 100_000, "claude"),
    ]
    chosen = select_tiers_to_shed(
        classifications, available_kb=50_000, total_kb=1_000_000, relief_threshold=_RELIEF
    )
    assert chosen == [Tier.AGENT_CHILD]


def test_select_tiers_escalates_when_cheap_tiers_do_not_free_enough() -> None:
    # Tier 8 and 7 are small; only after shedding both does projected usage clear
    # relief, so escalation reaches tier 7 but still stops before the user agent.
    classifications = [
        _classification(1, Tier.AGENT_CHILD, 100_000, "child"),
        _classification(2, Tier.WORKER_AGENT, 120_000, "worker"),
        _classification(3, Tier.USER_AGENT, 500_000, "claude"),
    ]
    chosen = select_tiers_to_shed(
        classifications, available_kb=50_000, total_kb=1_000_000, relief_threshold=_RELIEF
    )
    assert chosen == [Tier.AGENT_CHILD, Tier.WORKER_AGENT]


def test_select_tiers_skips_empty_tiers_but_keeps_escalating() -> None:
    # No agent-child processes exist; the worker tier is the cheapest available.
    classifications = [
        _classification(1, Tier.WORKER_AGENT, 800_000, "worker"),
        _classification(2, Tier.USER_AGENT, 100_000, "claude"),
    ]
    chosen = select_tiers_to_shed(
        classifications, available_kb=50_000, total_kb=1_000_000, relief_threshold=_RELIEF
    )
    assert chosen == [Tier.WORKER_AGENT]


def test_select_tiers_sheds_nothing_when_already_relieved() -> None:
    classifications = [_classification(1, Tier.AGENT_CHILD, 10_000, "child")]
    chosen = select_tiers_to_shed(
        classifications, available_kb=300_000, total_kb=1_000_000, relief_threshold=_RELIEF
    )
    assert chosen == []


def test_select_tiers_falls_back_to_user_agent_only_as_last_resort() -> None:
    # Everything cheaper is absent and the user agent is the sole holder; it is
    # selected only because nothing else can relieve the pressure.
    classifications = [_classification(1, Tier.USER_AGENT, 800_000, "claude")]
    chosen = select_tiers_to_shed(
        classifications, available_kb=50_000, total_kb=1_000_000, relief_threshold=_RELIEF
    )
    assert chosen == [Tier.USER_AGENT]


def test_select_shed_targets_filters_tier_largest_first() -> None:
    classifications = [
        _classification(1, Tier.AGENT_CHILD, 100, "small"),
        _classification(2, Tier.AGENT_CHILD, 500, "big"),
        _classification(3, Tier.USER_AGENT, 999, "agent"),
        _classification(4, Tier.AGENT_CHILD, 300, "medium"),
    ]
    targets = select_shed_targets(classifications, Tier.AGENT_CHILD)
    assert [t.pid for t in targets] == [2, 4, 1]


def test_summarize_recent_sheds_aggregates_by_label() -> None:
    records = [
        ShedRecord(
            timestamp="t",
            tier=Tier.AGENT_CHILD,
            tier_rank=8,
            label="pytest",
            pid=1,
            resident_kb=100,
            agent_name=None,
        ),
        ShedRecord(
            timestamp="t",
            tier=Tier.AGENT_CHILD,
            tier_rank=8,
            label="pytest",
            pid=2,
            resident_kb=300,
            agent_name=None,
        ),
        ShedRecord(
            timestamp="t",
            tier=Tier.WORKER_AGENT,
            tier_rank=7,
            label="worker",
            pid=3,
            resident_kb=50,
            agent_name="worker",
        ),
    ]
    summaries = summarize_recent_sheds(records)
    summary_by_label = {s.label: s for s in summaries}
    assert summary_by_label["pytest"].count == 2
    assert summary_by_label["pytest"].reclaimed_kb == 400
    assert summary_by_label["worker"].count == 1
    # Largest reclaimer comes first.
    assert summaries[0].label == "pytest"


def _wait_until_dead(process: subprocess.Popen, timeout_seconds: float) -> bool:
    """Block until the process exits or the timeout elapses (no busy-wait)."""
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        return False
    return True


def test_shed_tier_kills_real_process_in_tier() -> None:
    # start_new_session puts the child in its own process group, so the
    # shedder's same-group self-protection does not skip it.
    victim = subprocess.Popen(["sleep", "98765"], start_new_session=True)
    survivor = subprocess.Popen(["sleep", "98766"], start_new_session=True)
    try:
        classifications = [
            _classification(victim.pid, Tier.AGENT_CHILD, 1000, "victim"),
            _classification(survivor.pid, Tier.USER_AGENT, 1000, "survivor"),
        ]
        records = shed_tier(classifications, Tier.AGENT_CHILD)
        assert [r.pid for r in records] == [victim.pid]
        assert _wait_until_dead(victim, timeout_seconds=5.0)
        # A process in a different tier is untouched.
        assert survivor.poll() is None
    finally:
        for process in (victim, survivor):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def test_shed_tier_marks_agent_name_only_for_agent_tiers() -> None:
    victim = subprocess.Popen(["sleep", "98767"], start_new_session=True)
    try:
        records = shed_tier(
            [_classification(victim.pid, Tier.WORKER_AGENT, 1000, "worker7")],
            Tier.WORKER_AGENT,
        )
        assert len(records) == 1
        assert records[0].agent_name == "worker7"
        assert _wait_until_dead(victim, timeout_seconds=5.0)
    finally:
        if victim.poll() is None:
            victim.kill()
        victim.wait(timeout=5)
