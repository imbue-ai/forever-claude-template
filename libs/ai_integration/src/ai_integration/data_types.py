"""Frozen data types shared across the ai_integration library."""

import enum

from imbue.imbue_common.frozen_model import FrozenModel


class BillingPath(enum.Enum):
    """Which backend (and therefore billing bucket) served a call.

    ``DIRECT_API`` -- the direct Anthropic API, billed pay-per-token against the
    API account (``ANTHROPIC_API_KEY``). ``CLAUDE_CLI`` -- headless ``claude -p``,
    which draws the separate programmatic / Agent-SDK pool on a subscription (or
    the API account if a key is present in its env). Neither competes with the
    interactive chat pool.
    """

    DIRECT_API = "direct_api"
    CLAUDE_CLI = "claude_cli"


class Usage(FrozenModel):
    """Token counts for a single completion."""

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class CompletionResult(FrozenModel):
    """The result of a non-agentic completion (``run_completion``)."""

    text: str
    billing_path: BillingPath
    model: str
    usage: Usage | None = None
    # Actual cost when known (``claude -p`` reports it; direct API is estimated
    # from usage and the price table). ``None`` when it can't be determined.
    cost_usd: float | None = None


class AgentOutcome(enum.Enum):
    """Normalized outcome of a launched full agent (``run_agent``)."""

    DONE = "done"
    STUCK = "stuck"
    NO_UPDATE_NEEDED = "no-update-needed"
    TIMED_OUT = "timed_out"
    UNKNOWN = "unknown"


class AgentResult(FrozenModel):
    """Structured result of a launched full agent.

    ``outcome`` is the normalized enum; ``report_type``/``report_name`` are the
    raw report frontmatter; ``body`` is the prose the worker addressed to the
    user; ``branch`` is the worker's git branch (which survives agent teardown).
    """

    outcome: AgentOutcome
    report_type: str | None
    report_name: str | None
    body: str
    branch: str | None = None
    raw_report: str = ""
