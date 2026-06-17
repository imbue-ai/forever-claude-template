"""Unit tests for the error-output layer.

The `mngr` argv builders are additionally confronted with the live
`imbue.mngr.main.cli` tree via `assert_mngr_argv_valid`, so a vendor/mngr rename
of the `list`/`message` subcommand or one of its flags fails here at merge time.
"""

import json
import random

from mngr_cli_contract.contract import assert_mngr_argv_valid

from error_watcher.outputs import (
    AgentSummary,
    ErrorAlert,
    RandomMngrAgentErrorOutput,
    build_list_command,
    build_message_command,
    format_alert,
    parse_agent_summaries,
    select_messageable_names,
)
from error_watcher.testing import FakeCommandRunner

# Two agents that can both receive a message; with random.Random(0) the chosen
# recipient over ["agent-web", "agent-api"] is deterministically "agent-api".
_TWO_MESSAGEABLE_AGENTS = json.dumps(
    {
        "agents": [
            {"name": "agent-web", "type": "claude", "state": "RUNNING"},
            {"name": "agent-api", "type": "claude", "state": "WAITING"},
        ],
        "errors": [],
    }
)

_ONE_MESSAGEABLE_AGENT = json.dumps(
    {
        "agents": [{"name": "agent-solo", "type": "claude", "state": "RUNNING"}],
        "errors": [],
    }
)

_ONLY_STOPPED_AGENT = json.dumps(
    {
        "agents": [{"name": "agent-web", "type": "claude", "state": "STOPPED"}],
        "errors": [],
    }
)


def _alert() -> ErrorAlert:
    return ErrorAlert(origin="agent-session", matches_by_source={"svc-web": ["Exception: boom"]})


def _delivery_runner(
    list_stdout: str,
    sends: list[list[str]],
    *,
    send_fails: bool = False,
    failing_recipients: frozenset[str] = frozenset(),
    list_returncode: int = 0,
) -> FakeCommandRunner:
    # The output layer never touches tmux, so the session/window fields are inert
    # here; only the mngr list/message behavior matters.
    return FakeCommandRunner(
        session="agent-session",
        windows=(),
        pane_text_by_window={},
        list_stdout=list_stdout,
        message_sends=sends,
        send_fails=send_fails,
        failing_recipients=failing_recipients,
        list_returncode=list_returncode,
    )


# --- Formatting ---


def test_format_alert_includes_origin_source_and_line() -> None:
    message = format_alert("agent-session", {"svc-web": ["Error: boom"]})
    assert "agent-session" in message
    assert "svc-web" in message
    assert "Error: boom" in message


def test_format_alert_batches_multiple_sources_into_one_message() -> None:
    message = format_alert(
        "agent-session",
        {"svc-web": ["Error: boom"], "svc-api": ["Exception: a", "Exception: b"]},
    )
    assert "svc-web" in message
    assert "svc-api" in message
    assert "Exception: a | Exception: b" in message


def test_format_alert_truncates_overlong_lines() -> None:
    long_line = "Error " + "x" * 1000
    message = format_alert("agent-session", {"svc-web": [long_line]})
    assert "..." in message
    assert len(long_line) not in {len(part) for part in message.splitlines()}


# --- mngr argv builders (validated against the live CLI) ---


def test_list_command_is_accepted_by_live_cli() -> None:
    argv = build_list_command()
    assert argv == ["mngr", "list", "--format", "json"]
    assert_mngr_argv_valid(argv)


def test_message_command_is_accepted_by_live_cli() -> None:
    argv = build_message_command("demo-agent", "something errored")
    assert argv == ["mngr", "message", "demo-agent", "-m", "something errored"]
    assert_mngr_argv_valid(argv)


# --- Agent enumeration ---


def test_parse_agent_summaries_reads_name_state_and_type() -> None:
    payload = json.dumps(
        {
            "agents": [
                {"name": "agent-web", "type": "claude", "state": "RUNNING"},
                {"name": "agent-api", "type": "claude", "state": "STOPPED"},
            ],
            "errors": [],
        }
    )
    assert parse_agent_summaries(payload) == [
        AgentSummary(name="agent-web", state="RUNNING", agent_type="claude"),
        AgentSummary(name="agent-api", state="STOPPED", agent_type="claude"),
    ]


def test_parse_agent_summaries_skips_agents_missing_name_or_state() -> None:
    payload = json.dumps(
        {
            "agents": [
                {"name": "agent-web", "state": "RUNNING"},
                {"name": "", "state": "RUNNING"},
                {"name": "agent-api"},
                "not-a-dict",
            ]
        }
    )
    # A missing `type` becomes "" rather than dropping the agent; the type
    # filter is applied later by select_messageable_names.
    assert parse_agent_summaries(payload) == [
        AgentSummary(name="agent-web", state="RUNNING", agent_type="")
    ]


def test_parse_agent_summaries_returns_empty_on_malformed_json() -> None:
    assert parse_agent_summaries("this is not json") == []


def test_parse_agent_summaries_returns_empty_when_not_an_object() -> None:
    assert parse_agent_summaries("[1, 2, 3]") == []


def test_parse_agent_summaries_returns_empty_when_agents_not_a_list() -> None:
    assert parse_agent_summaries(json.dumps({"agents": "nope"})) == []


def test_select_messageable_names_excludes_stopped_claude_agents() -> None:
    agents = [
        AgentSummary(name="run", state="RUNNING", agent_type="claude"),
        AgentSummary(name="wait", state="WAITING", agent_type="claude"),
        AgentSummary(name="stop", state="STOPPED", agent_type="claude"),
    ]
    assert select_messageable_names(agents) == ["run", "wait"]


def test_select_messageable_names_excludes_non_claude_agents() -> None:
    # The `main`-type system-services agent has no interactive claude inbox, so
    # it must never be chosen even when running (REQ-NOTIFY-3).
    agents = [
        AgentSummary(name="agent-web", state="RUNNING", agent_type="claude"),
        AgentSummary(name="system-services", state="RUNNING", agent_type="main"),
    ]
    assert select_messageable_names(agents) == ["agent-web"]


def test_select_messageable_names_empty_when_all_stopped() -> None:
    assert (
        select_messageable_names(
            [AgentSummary(name="stop", state="STOPPED", agent_type="claude")]
        )
        == []
    )


# --- Random recipient policy ---


def test_choose_recipients_orders_the_pool_uniformly_at_random() -> None:
    output = RandomMngrAgentErrorOutput(_delivery_runner("", []), random.Random(0))
    # A deterministic permutation for a seeded rng; the first element is the
    # uniform-random first pick and the rest are the fallback order.
    assert output.choose_recipients(["alpha", "beta", "gamma"]) == ["beta", "gamma", "alpha"]


def test_choose_recipients_returns_empty_for_empty_pool() -> None:
    output = RandomMngrAgentErrorOutput(_delivery_runner("", []), random.Random(0))
    assert output.choose_recipients([]) == []


# --- Delivery ---


def test_deliver_sends_one_alert_to_a_messageable_agent() -> None:
    sends: list[list[str]] = []
    output = RandomMngrAgentErrorOutput(
        _delivery_runner(_TWO_MESSAGEABLE_AGENTS, sends), random.Random(0)
    )
    assert output.deliver(_alert()) == "agent-api"
    assert len(sends) == 1
    argv = sends[0]
    assert argv[:3] == ["mngr", "message", "agent-api"]
    assert "svc-web" in argv[-1]
    assert "Exception: boom" in argv[-1]


def test_deliver_returns_none_and_sends_nothing_when_no_messageable_agent() -> None:
    sends: list[list[str]] = []
    output = RandomMngrAgentErrorOutput(
        _delivery_runner(_ONLY_STOPPED_AGENT, sends), random.Random(0)
    )
    assert output.deliver(_alert()) is None
    assert sends == []


def test_deliver_falls_back_to_another_agent_when_a_send_fails() -> None:
    # With random.Random(0) the first pick over two agents is "agent-api"; that
    # send fails, so delivery must fall back to "agent-web" rather than be lost.
    sends: list[list[str]] = []
    output = RandomMngrAgentErrorOutput(
        _delivery_runner(
            _TWO_MESSAGEABLE_AGENTS, sends, failing_recipients=frozenset({"agent-api"})
        ),
        random.Random(0),
    )
    assert output.deliver(_alert()) == "agent-web"
    assert [argv[2] for argv in sends] == ["agent-api", "agent-web"]


def test_deliver_tries_every_agent_before_giving_up() -> None:
    sends: list[list[str]] = []
    output = RandomMngrAgentErrorOutput(
        _delivery_runner(_TWO_MESSAGEABLE_AGENTS, sends, send_fails=True),
        random.Random(0),
    )
    assert output.deliver(_alert()) is None
    assert sorted(argv[2] for argv in sends) == ["agent-api", "agent-web"]


def test_deliver_uses_a_valid_payload_even_when_list_exits_nonzero() -> None:
    # mngr can exit non-zero (e.g. one provider failed) while still printing a
    # valid {"agents": [...]} body; the alert must not be skipped (finding #6).
    sends: list[list[str]] = []
    output = RandomMngrAgentErrorOutput(
        _delivery_runner(_TWO_MESSAGEABLE_AGENTS, sends, list_returncode=1),
        random.Random(0),
    )
    assert output.deliver(_alert()) == "agent-api"
    assert len(sends) == 1


def test_deliver_returns_none_when_list_fails_without_a_payload() -> None:
    sends: list[list[str]] = []
    output = RandomMngrAgentErrorOutput(
        _delivery_runner("mngr: connection refused", sends, list_returncode=1),
        random.Random(0),
    )
    assert output.deliver(_alert()) is None
    assert sends == []


def test_deliver_sends_to_a_single_agent_pool() -> None:
    sends: list[list[str]] = []
    output = RandomMngrAgentErrorOutput(
        _delivery_runner(_ONE_MESSAGEABLE_AGENT, sends), random.Random(0)
    )
    assert output.deliver(_alert()) == "agent-solo"
    assert len(sends) == 1
