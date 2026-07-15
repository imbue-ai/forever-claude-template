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
    "github-sync",
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


def test_unrecognized_supervisord_program_falls_back_to_the_user_service_band() -> None:
    # The core fail-expendable guarantee: a program the policy does not know
    # (a user-created service that skipped the tagging prefix) must default to
    # the user-service band, never to a protected one.
    assert bands.supervisord_program_band("some-user-service") == bands.USER_SERVICE


def test_supervisord_program_bands_preserve_the_shedding_order() -> None:
    # Program names that double as service keys resolve to their service band;
    # the OOM machinery itself stays protected; the browser stays the single
    # most-expendable thing, above even an agent's subprocesses.
    for key in _BUILTIN_SERVICE_ORDER:
        assert bands.supervisord_program_band(key) == bands.SERVICE_BANDS[key]
    assert bands.supervisord_program_band("earlyoom") == bands.PROTECTED
    assert bands.supervisord_program_band("oom-tag-backstop") == bands.PROTECTED
    assert bands.SHARED_BROWSER > bands.AGENT_SUBPROCESS
    assert bands.supervisord_program_band("browser") == bands.SHARED_BROWSER


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
    engaged = bands.chat_agent_oom_score_adj(
        is_open=True, is_visible=True, recency_rank=0
    )
    idle = bands.chat_agent_oom_score_adj(
        is_open=False, is_visible=False, recency_rank=None
    )
    assert engaged == bands.CHAT_AGENT_FLOOR
    assert idle == bands.CHAT_AGENT_BASE
    # A fully engaged chat is the most protected; a closed, never-messaged one the least.
    assert engaged < idle


def test_never_messaged_chat_gets_no_recency_bonus() -> None:
    # ``None`` (never messaged) must not be treated as the most-recent (rank 0):
    # an open+visible chat that was never messaged is less protected than one that
    # was just messaged.
    never = bands.chat_agent_oom_score_adj(
        is_open=True, is_visible=True, recency_rank=None
    )
    just_messaged = bands.chat_agent_oom_score_adj(
        is_open=True, is_visible=True, recency_rank=0
    )
    assert just_messaged < never


def test_chat_score_monotonic_in_each_signal() -> None:
    base = bands.chat_agent_oom_score_adj(
        is_open=False, is_visible=False, recency_rank=5
    )
    opened = bands.chat_agent_oom_score_adj(
        is_open=True, is_visible=False, recency_rank=5
    )
    visible = bands.chat_agent_oom_score_adj(
        is_open=True, is_visible=True, recency_rank=5
    )
    more_recent = bands.chat_agent_oom_score_adj(
        is_open=False, is_visible=False, recency_rank=2
    )
    never = bands.chat_agent_oom_score_adj(
        is_open=False, is_visible=False, recency_rank=None
    )
    # Each engagement signal only ever lowers (more-protects) the score.
    assert opened < base
    assert visible < opened
    assert more_recent < base
    # Any messaged rank is at least as protected as never-messaged.
    assert base <= never


def test_browser_remap_lands_inside_the_band_and_preserves_chromes_order() -> None:
    # Chrome's self-assigned gradation (browser/zygote 0, gpu/utility 200,
    # renderers 300, up to 1000) must map to strictly increasing values that all
    # sit inside the browser band's range -- i.e. above every agent subprocess.
    remapped = [bands.shared_browser_oom_score_adj(v) for v in (0, 200, 300, 1000)]
    assert remapped == sorted(remapped)
    assert len(set(remapped)) == len(remapped), "Chrome's gradation must survive the remap"
    for value in remapped:
        assert bands.SHARED_BROWSER_FLOOR <= value <= bands.SHARED_BROWSER
    assert bands.AGENT_SUBPROCESS < bands.SHARED_BROWSER_FLOOR < bands.SHARED_BROWSER


def test_browser_remap_output_is_never_below_the_floor() -> None:
    # The browser service's sweep only remaps values *below* the floor; the remap
    # emitting values at/above the floor is what makes repeated sweeps idempotent
    # (a remapped process is never remapped again). Out-of-range inputs clamp.
    for value in (-1000, -1, 0, 1, 299, 300, 999, 1000, 2000):
        remapped = bands.shared_browser_oom_score_adj(value)
        assert bands.SHARED_BROWSER_FLOOR <= remapped <= bands.SHARED_BROWSER, value


def test_chat_score_always_within_the_chat_band() -> None:
    for is_open in (True, False):
        for is_visible in (True, False):
            for rank in (0, 1, 3, 10, 100, None):
                adj = bands.chat_agent_oom_score_adj(
                    is_open=is_open, is_visible=is_visible, recency_rank=rank
                )
                assert bands.CHAT_AGENT_FLOOR <= adj <= bands.CHAT_AGENT_BASE
                assert bands.USER_SERVICE < adj < bands.WORKER_AGENT
