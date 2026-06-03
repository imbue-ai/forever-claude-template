"""Exceptions for the ai_integration library.

Each concrete error inherits the package base (so callers can catch the whole
family) plus the closest built-in, so existing ``except RuntimeError`` / etc.
handlers still see them.
"""


class AIIntegrationError(Exception):
    """Base class for all ai_integration errors."""


class CredentialsUnavailableError(AIIntegrationError, RuntimeError):
    """No usable Claude credential path could be resolved.

    Raised (loudly, rather than silently degrading) when neither an
    ``ANTHROPIC_API_KEY`` nor a Claude config dir with credentials is available,
    so a service fails with a clear message instead of an opaque auth error from
    deep inside ``claude -p``.
    """


class SpendCeilingExceededError(AIIntegrationError, RuntimeError):
    """The configured per-service spend ceiling was reached.

    Raised before making a paid call once cumulative spend in the window meets or
    exceeds the ceiling, so volume can never silently run past the budget.
    """


class ClaudeCLIError(AIIntegrationError, RuntimeError):
    """A ``claude -p`` invocation failed or returned unparseable output."""


class AgentRunError(AIIntegrationError, RuntimeError):
    """A launched full agent failed, timed out, or its report did not parse."""
