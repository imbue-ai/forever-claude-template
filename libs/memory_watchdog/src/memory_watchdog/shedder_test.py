import subprocess

from memory_watchdog.data_types import ProcessClassification, ShedRecord, Tier
from memory_watchdog.shedder import (
    select_processes_to_shed,
    shed_processes,
    summarize_recent_sheds,
)

# Relief at 80% used: the watchdog stops shedding once projected usage drops
# below this. Matches watchdog.SHED_RELIEF_THRESHOLD.
_RELIEF = 0.80
# Resident-size floor below which a process is never shed. Matches
# watchdog.MIN_SHEDDABLE_RSS_KB (10 MiB).
_FLOOR = 10 * 1024


def _classification(
    pid: int,
    tier: Tier,
    resident_kb: int,
    label: str,
    owning_agent_name: str | None = None,
) -> ProcessClassification:
    return ProcessClassification(
        pid=pid,
        resident_kb=resident_kb,
        tier=tier,
        label=label,
        owning_agent_name=owning_agent_name,
    )


def _pids(chosen: list[ProcessClassification]) -> list[int]:
    return [c.pid for c in chosen]


def test_sheds_only_the_hog_and_stops_when_it_frees_enough() -> None:
    # An 800MB agent-child hog under acute pressure (50MB available of 1000MB).
    # Shedding it alone projects usage down to 15%, so the user's agent (tier 5)
    # must NOT be selected -- the over-shed regression guard.
    classifications = [
        _classification(1, Tier.AGENT_CHILD, 800_000, "hog"),
        _classification(2, Tier.USER_AGENT, 100_000, "claude"),
    ]
    chosen = select_processes_to_shed(
        classifications,
        available_kb=50_000,
        total_kb=1_000_000,
        relief_threshold=_RELIEF,
        min_resident_kb=_FLOOR,
    )
    assert _pids(chosen) == [1]


def test_spares_small_helpers_sharing_the_hogs_tier() -> None:
    # The collateral-damage regression: a big agent-child hog plus a swarm of
    # tiny tier-8 helpers (transcript streamer, a lead's report poll, a sleep).
    # Only the hog is shed; the helpers, the worker's own claude, and the lead's
    # agent all survive -- shedding the hog alone already clears relief, and the
    # sub-floor helpers could not have helped anyway.
    classifications = [
        _classification(1, Tier.AGENT_CHILD, 800_000, "python3 hog.py", "worker"),
        _classification(
            2, Tier.AGENT_CHILD, 3_000, "bash stream_transcript.sh", "worker"
        ),
        _classification(
            3, Tier.AGENT_CHILD, 11_000, "python3 create_worker.py", "lead"
        ),
        _classification(4, Tier.AGENT_CHILD, 1_100, "sleep", "worker"),
        _classification(5, Tier.WORKER_AGENT, 300_000, "worker", "worker"),
        _classification(6, Tier.USER_AGENT, 250_000, "lead", "lead"),
    ]
    chosen = select_processes_to_shed(
        classifications,
        available_kb=50_000,
        total_kb=1_000_000,
        relief_threshold=_RELIEF,
        min_resident_kb=_FLOOR,
    )
    assert _pids(chosen) == [1]


def test_escalates_across_tiers_but_stops_before_user_agent() -> None:
    # Tier 8 and 7 are small; only after shedding both does projected usage clear
    # relief, so escalation reaches the worker tier but still stops before the
    # user agent (tier 5).
    classifications = [
        _classification(1, Tier.AGENT_CHILD, 100_000, "child"),
        _classification(2, Tier.WORKER_AGENT, 120_000, "worker"),
        _classification(3, Tier.USER_AGENT, 500_000, "claude"),
    ]
    chosen = select_processes_to_shed(
        classifications,
        available_kb=50_000,
        total_kb=1_000_000,
        relief_threshold=_RELIEF,
        min_resident_kb=_FLOOR,
    )
    assert _pids(chosen) == [1, 2]


def test_orders_by_tier_then_largest_resident_first() -> None:
    # Pressure so acute that even shedding everything cannot reach relief, so all
    # candidates are selected -- letting us assert the ORDER: lower tier first
    # (agent-child before worker), largest resident set first within a tier.
    classifications = [
        _classification(1, Tier.AGENT_CHILD, 30_000, "small-child"),
        _classification(2, Tier.AGENT_CHILD, 50_000, "big-child"),
        _classification(3, Tier.WORKER_AGENT, 70_000, "worker"),
    ]
    chosen = select_processes_to_shed(
        classifications,
        available_kb=10_000,
        total_kb=1_000_000,
        relief_threshold=_RELIEF,
        min_resident_kb=_FLOOR,
    )
    assert _pids(chosen) == [2, 1, 3]


def test_sheds_nothing_when_already_relieved() -> None:
    classifications = [_classification(1, Tier.AGENT_CHILD, 50_000, "child")]
    chosen = select_processes_to_shed(
        classifications,
        available_kb=300_000,
        total_kb=1_000_000,
        relief_threshold=_RELIEF,
        min_resident_kb=_FLOOR,
    )
    assert chosen == []


def test_user_agent_is_last_resort() -> None:
    # Everything cheaper is absent and the user agent is the sole holder; it is
    # selected only because nothing else can relieve the pressure.
    classifications = [_classification(1, Tier.USER_AGENT, 800_000, "claude")]
    chosen = select_processes_to_shed(
        classifications,
        available_kb=50_000,
        total_kb=1_000_000,
        relief_threshold=_RELIEF,
        min_resident_kb=_FLOOR,
    )
    assert _pids(chosen) == [1]


def test_negligible_processes_are_never_shed() -> None:
    # Under acute pressure but the only processes are sub-floor helpers: shedding
    # any of them frees too little to matter, so nothing is shed (no pointless
    # collateral). The next poll / the kernel OOM killer is the backstop.
    classifications = [
        _classification(1, Tier.AGENT_CHILD, 2_000, "sleep"),
        _classification(2, Tier.AGENT_CHILD, 3_000, "bash stream_transcript.sh"),
    ]
    chosen = select_processes_to_shed(
        classifications,
        available_kb=20_000,
        total_kb=1_000_000,
        relief_threshold=_RELIEF,
        min_resident_kb=_FLOOR,
    )
    assert chosen == []


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


def test_summarize_groups_by_owning_agent() -> None:
    # The same command run by two agents must stay on separate lines so each can
    # name its owning agent, rather than collapsing into one ambiguous "python3".
    records = [
        ShedRecord(
            timestamp="t",
            tier=Tier.AGENT_CHILD,
            tier_rank=8,
            label="python3 hog.py",
            pid=1,
            resident_kb=2000,
            agent_name=None,
            owning_agent_name="alice",
        ),
        ShedRecord(
            timestamp="t",
            tier=Tier.AGENT_CHILD,
            tier_rank=8,
            label="python3 hog.py",
            pid=2,
            resident_kb=1000,
            agent_name=None,
            owning_agent_name="bob",
        ),
    ]
    summaries = summarize_recent_sheds(records)
    assert len(summaries) == 2
    by_agent = {s.owning_agent_name: s for s in summaries}
    assert by_agent["alice"].count == 1
    assert by_agent["alice"].label == "python3 hog.py"
    assert by_agent["bob"].count == 1


def test_shed_processes_records_owning_agent_for_subprocess() -> None:
    # A shed subprocess is attributed to its agent (owning_agent_name) but is not
    # itself an agent shed (agent_name stays None, so no revival notice fires).
    victim = subprocess.Popen(["sleep", "98768"], start_new_session=True)
    try:
        records = shed_processes(
            [
                _classification(
                    victim.pid, Tier.AGENT_CHILD, 1000, "python3 hog.py", "alice"
                )
            ]
        )
        assert len(records) == 1
        assert records[0].agent_name is None
        assert records[0].owning_agent_name == "alice"
        assert _wait_until_dead(victim, timeout_seconds=5.0)
    finally:
        if victim.poll() is None:
            victim.kill()
        victim.wait(timeout=5)


def _wait_until_dead(process: subprocess.Popen, timeout_seconds: float) -> bool:
    """Block until the process exits or the timeout elapses (no busy-wait)."""
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        return False
    return True


def test_shed_processes_kills_only_the_given_targets() -> None:
    # start_new_session puts each child in its own process group, so the
    # shedder's same-group self-protection does not skip it.
    victim = subprocess.Popen(["sleep", "98765"], start_new_session=True)
    survivor = subprocess.Popen(["sleep", "98766"], start_new_session=True)
    try:
        records = shed_processes(
            [_classification(victim.pid, Tier.AGENT_CHILD, 1000, "victim")]
        )
        assert [r.pid for r in records] == [victim.pid]
        assert _wait_until_dead(victim, timeout_seconds=5.0)
        # A process not in the target list is untouched.
        assert survivor.poll() is None
    finally:
        for process in (victim, survivor):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def test_shed_processes_marks_agent_name_only_for_agent_tiers() -> None:
    victim = subprocess.Popen(["sleep", "98767"], start_new_session=True)
    try:
        records = shed_processes(
            [_classification(victim.pid, Tier.WORKER_AGENT, 1000, "worker7")]
        )
        assert len(records) == 1
        assert records[0].agent_name == "worker7"
        assert _wait_until_dead(victim, timeout_seconds=5.0)
    finally:
        if victim.poll() is None:
            victim.kill()
        victim.wait(timeout=5)
