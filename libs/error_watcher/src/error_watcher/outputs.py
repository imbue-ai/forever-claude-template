"""Error-output layer: decides where an alert goes and delivers it.

`ErrorOutput` is the layer seam -- swap mngr for any other delivery channel.
`MngrAgentErrorOutput` delivers an alert as an `mngr message` and leaves *which*
agent(s) to target to an overridable `choose_recipients` policy;
`RandomMngrAgentErrorOutput` is the default uniform-random policy. Replacing the
recipient choice later (e.g. routing to the agent best placed to fix the error)
is a one-method subclass, with the delivery mechanics unchanged.
"""

import json
import random
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Final, NamedTuple

from loguru import logger

from error_watcher.commands import CommandRunner

# Each matching line is truncated to this length in the alert so a single giant
# traceback line cannot blow up the message sent to the agent.
MAX_ALERT_LINE_LENGTH: Final[int] = 500

# mngr refuses to message an agent in this lifecycle state (REQ-NOTIFY-3): its
# send path (vendor/mngr/.../api/message.py) rejects only STOPPED agents, since
# only they lack a tmux session to receive the message.
STOPPED_STATE: Final[str] = "STOPPED"

# Only `type: claude` agents are messaged. This mirrors system_interface's
# list_claude_agent_names (apps/system_interface/.../claude_auth.py), which
# filters to claude agents to exclude the `main`-type system-services agent --
# that agent has no interactive claude process and no human watching its inbox,
# so alerting it would be a wasted nudge.
CLAUDE_AGENT_TYPE: Final[str] = "claude"


class AgentSummary(NamedTuple):
    """One agent from `mngr list --format json`, reduced to the fields we need.

    `state` is the agent's lifecycle state string (e.g. RUNNING, WAITING,
    STOPPED) and `agent_type` is its type (e.g. claude, main); the messageable
    filter keys off both.
    """

    name: str
    state: str
    agent_type: str


class ErrorAlert(NamedTuple):
    """The batched alert the routing layer hands to the output layer.

    `origin` is where the errors were seen (e.g. the tmux session name) and
    `matches_by_source` maps each source name to its newly-matched line(s). The
    output layer renders and delivers this however it sees fit.
    """

    origin: str
    matches_by_source: Mapping[str, Sequence[str]]


def build_list_command() -> list[str]:
    """Build the `mngr list` argv used to enumerate agents."""
    return ["mngr", "list", "--format", "json"]


def build_message_command(agent_name: str, message: str) -> list[str]:
    """Build the `mngr message` argv used to alert one agent."""
    return ["mngr", "message", agent_name, "-m", message]


def parse_agent_summaries(stdout: str) -> list[AgentSummary]:
    """Parse `mngr list --format json` output into name/state summaries.

    The CLI emits `{"agents": [{"name": ..., "state": ..., "type": ...}], ...}`.
    Tolerant by design (REQ-SPAWN-4): malformed or unexpected output yields an
    empty list plus a warning so the poll loop never crashes. Agents missing a
    usable name or state are skipped; a missing or non-string `type` becomes ""
    (and is later filtered out as non-claude rather than messaged blindly).
    """
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as e:
        logger.warning(
            "Skipped agent enumeration: mngr list output was not valid JSON: {}", e
        )
        return []
    if not isinstance(payload, dict):
        logger.warning(
            "Skipped agent enumeration: mngr list output was not a JSON object: {!r}",
            payload,
        )
        return []
    agents = payload.get("agents", [])
    if not isinstance(agents, list):
        logger.warning(
            "Skipped agent enumeration: mngr list 'agents' field was not a list: {!r}",
            agents,
        )
        return []
    summaries: list[AgentSummary] = []
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        name = agent.get("name")
        state = agent.get("state")
        agent_type = agent.get("type")
        if isinstance(name, str) and name and isinstance(state, str) and state:
            type_str = agent_type if isinstance(agent_type, str) else ""
            summaries.append(AgentSummary(name=name, state=state, agent_type=type_str))
    return summaries


def select_messageable_names(agents: Sequence[AgentSummary]) -> list[str]:
    """Return the names of agents that can currently receive a useful message.

    Two filters, aligned with mngr's real deliverability and the cited
    reference (REQ-NOTIFY-3):

    - STOPPED agents are excluded -- mngr's send path refuses only STOPPED
      agents (they have no tmux session), and the watcher never starts a
      stopped agent just to alert it. Other lifecycle states are left in: mngr
      itself attempts delivery to them, and a transient failure is now handled
      by the in-poll fallback across the rest of the pool rather than by
      pre-filtering states the spec does not call out.
    - Only `type: claude` agents are kept, mirroring
      list_claude_agent_names, so the non-interactive `main`-type
      system-services agent is never picked as a recipient.
    """
    return [
        agent.name
        for agent in agents
        if agent.state != STOPPED_STATE and agent.agent_type == CLAUDE_AGENT_TYPE
    ]


def _truncate_line(line: str) -> str:
    if len(line) <= MAX_ALERT_LINE_LENGTH:
        return line
    return line[:MAX_ALERT_LINE_LENGTH] + "..."


def format_alert(origin: str, matches_by_source: Mapping[str, Sequence[str]]) -> str:
    """Build one human-readable alert covering every source that newly matched this poll.

    A single message names each source and includes its matching line(s), so
    multiple sources erroring in one poll yield one batched message rather than
    one per source (REQ-NOTIFY-2, REQ-NOTIFY-6).
    """
    header = f"Possible error/exception detected by error-watcher in '{origin}':"
    source_lines = [
        f"- '{name}': {' | '.join(_truncate_line(line) for line in lines)}"
        for name, lines in matches_by_source.items()
    ]
    return "\n".join([header, *source_lines])


class ErrorOutput(ABC):
    """Error-output layer: delivers a batched alert to wherever it should go."""

    @abstractmethod
    def deliver(self, alert: ErrorAlert) -> str | None:
        """Deliver the alert, returning a delivery id (e.g. recipient) or None if undelivered."""


class MngrAgentErrorOutput(ErrorOutput, ABC):
    """Delivers an alert as an `mngr message`; the recipient choice is left to a policy.

    `deliver` owns the fixed mechanics -- enumerate messageable agents, format
    the alert, send with fallback across the chosen order -- while
    `choose_recipients` (abstract) owns the policy of *which* agent(s) to try and
    in what order. This separates "how the alert is delivered" from "where it
    goes", so a future error-fixing policy is a subclass that overrides only
    `choose_recipients`.
    """

    def __init__(self, run: CommandRunner) -> None:
        self._run = run

    @abstractmethod
    def choose_recipients(self, candidates: Sequence[str]) -> list[str]:
        """Order the messageable candidates: the first is tried first, the rest are fallbacks."""

    def deliver(self, alert: ErrorAlert) -> str | None:
        message = format_alert(alert.origin, alert.matches_by_source)
        list_result = self._run(build_list_command())
        # Parse the payload regardless of exit status: mngr can exit non-zero
        # (e.g. one provider failed) while still emitting a valid
        # {"agents": [...]} body, and dropping that would needlessly skip the
        # alert (finding #6). Only treat a non-zero exit as fatal when it left us
        # with no usable agents.
        agents = parse_agent_summaries(list_result.stdout)
        if list_result.returncode != 0 and not agents:
            logger.warning(
                "Could not enumerate agents to alert: {}", list_result.stderr.strip()
            )
            return None
        candidates = select_messageable_names(agents)
        if not candidates:
            logger.warning(
                "Detected new error output but found no messageable agent to alert"
            )
            return None
        # Try recipients in policy order; on a failed send fall back to the next
        # so one bad pick -- e.g. an agent that stopped between `mngr list` and
        # `mngr message` -- does not drop the alert while others are reachable.
        for recipient in self.choose_recipients(candidates):
            send_result = self._run(build_message_command(recipient, message))
            if send_result.returncode == 0:
                logger.info("Alerted agent {} about new error output", recipient)
                return recipient
            logger.warning(
                "Failed to alert agent {}: {}", recipient, send_result.stderr.strip()
            )
        logger.warning(
            "Detected new error output but every messageable agent failed to receive the alert"
        )
        return None


class RandomMngrAgentErrorOutput(MngrAgentErrorOutput):
    """Picks recipients uniformly at random (the source agent is itself eligible, REQ-NOTIFY-5)."""

    def __init__(self, run: CommandRunner, rng: random.Random) -> None:
        super().__init__(run)
        self._rng = rng

    def choose_recipients(self, candidates: Sequence[str]) -> list[str]:
        # Uniform random first pick, then uniform random over the rest, so a
        # failed send falls back across the remaining pool without bias.
        remaining = list(candidates)
        order: list[str] = []
        while remaining:
            pick = self._rng.choice(remaining)
            remaining.remove(pick)
            order.append(pick)
        return order
