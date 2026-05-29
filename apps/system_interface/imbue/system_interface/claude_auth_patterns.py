"""Regex patterns that flag a Claude auth failure in assistant transcript text.

Sourced from the official Claude Code errors reference
(code.claude.com/docs/en/errors) plus common surface forms seen in
practice. Kept in a dedicated module so the list can be extended without
touching parser logic.
"""

from __future__ import annotations

import re

_PATTERN_SOURCES: tuple[str, ...] = (
    r"Not logged in\s*[\u00b7\u2022\-]\s*Please run /login",
    r"Invalid API key",
    r"OAuth token (?:has been revoked|has expired|does not meet scope requirements?)",
    r'"type"\s*:\s*"authentication_error"',
    r"API Error:\s*401\b",
    r"Invalid authentication credentials",
    r"Credit balance is too low",
    r"organization has been disabled",
)

AUTH_ERROR_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(source, re.IGNORECASE) for source in _PATTERN_SOURCES
)


def is_auth_error_text(text: str) -> bool:
    """Return True if any known Claude auth-error pattern appears in `text`."""
    if not text:
        return False
    for pattern in AUTH_ERROR_PATTERNS:
        if pattern.search(text):
            return True
    return False
