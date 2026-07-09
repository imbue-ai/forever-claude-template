"""Invariants for the memory-shedding priority bands.

These guard the graceful-degradation guarantee against an accidental edit to the
band values: services must stay below agents, user-created services must sit
above every built-in service but below the agent bands, and the built-in
services must keep the documented least- to most-expendable order.
"""

from oom_priority import bands

# The built-in services in their documented order, least- to most-expendable.
# User-created services (the "user" key) are excluded -- they are asserted
# separately as sitting above every one of these.
_BUILTIN_SERVICE_ORDER = (
    "terminal",
    "system_interface",
    "cloudflared",
    "runtime-backup",
    "host-backup",
    "app-watcher",
    "web",
)


def test_builtin_services_are_strictly_ordered_least_to_most_expendable() -> None:
    values = [bands.SERVICE_BANDS[key] for key in _BUILTIN_SERVICE_ORDER]
    assert values == sorted(values), values
    assert len(set(values)) == len(values), "built-in service bands must be distinct"


def test_every_service_band_sits_between_protected_and_the_user_agent() -> None:
    # A service is less expendable than any agent (agents revive on the next
    # message, so they are shed first) but more expendable than the never-kill
    # infrastructure at PROTECTED.
    for key, adj in bands.SERVICE_BANDS.items():
        assert bands.PROTECTED < adj < bands.USER_AGENT, (key, adj)


def test_user_created_services_are_shed_before_every_builtin_service() -> None:
    user_band = bands.SERVICE_BANDS["user"]
    assert user_band == bands.USER_SERVICE
    for key in _BUILTIN_SERVICE_ORDER:
        assert bands.SERVICE_BANDS[key] < user_band, key


def test_the_builtin_key_set_matches_the_documented_order() -> None:
    # Catch a service added to SERVICE_BANDS without being placed in the ordering
    # above (which would leave its rank unasserted).
    assert set(bands.SERVICE_BANDS) == {*_BUILTIN_SERVICE_ORDER, "user"}


def test_primary_agent_is_pinned_to_the_never_shed_band() -> None:
    # The primary (services) agent must be at least as protected as the never-kill
    # infrastructure, and strictly below every service and agent band, so it is
    # shed dead last.
    assert bands.PRIMARY_AGENT == bands.PROTECTED
    assert bands.PRIMARY_AGENT < min(bands.SERVICE_BANDS.values())
    assert bands.PRIMARY_AGENT < bands.USER_AGENT


def test_chat_band_range_sits_strictly_between_services_and_workers() -> None:
    # Every chat, however engaged, stays below WORKER_AGENT (workers are shed
    # first) and above the user-service band (a chat revives on its next message,
    # so it is shed before a service).
    assert bands.USER_SERVICE < bands.CHAT_AGENT_FLOOR
    assert bands.CHAT_AGENT_FLOOR < bands.CHAT_AGENT_BASE < bands.WORKER_AGENT


def test_chat_score_is_most_protected_when_fully_engaged() -> None:
    engaged = bands.chat_agent_oom_score_adj(is_open=True, is_visible=True, recency_rank=0)
    idle = bands.chat_agent_oom_score_adj(is_open=False, is_visible=False, recency_rank=99)
    assert engaged == bands.CHAT_AGENT_FLOOR
    assert idle == bands.CHAT_AGENT_BASE
    # A fully engaged chat is the most protected; a closed, stale one the least.
    assert engaged < idle


def test_chat_score_monotonic_in_each_signal() -> None:
    base = bands.chat_agent_oom_score_adj(is_open=False, is_visible=False, recency_rank=5)
    opened = bands.chat_agent_oom_score_adj(is_open=True, is_visible=False, recency_rank=5)
    visible = bands.chat_agent_oom_score_adj(is_open=True, is_visible=True, recency_rank=5)
    more_recent = bands.chat_agent_oom_score_adj(is_open=False, is_visible=False, recency_rank=2)
    # Each engagement signal only ever lowers (more-protects) the score.
    assert opened < base
    assert visible < opened
    assert more_recent < base


def test_chat_score_always_within_the_chat_band() -> None:
    for is_open in (True, False):
        for is_visible in (True, False):
            for rank in (0, 1, 3, 10, 100):
                adj = bands.chat_agent_oom_score_adj(is_open=is_open, is_visible=is_visible, recency_rank=rank)
                assert bands.CHAT_AGENT_FLOOR <= adj <= bands.CHAT_AGENT_BASE
                assert bands.USER_SERVICE < adj < bands.WORKER_AGENT
