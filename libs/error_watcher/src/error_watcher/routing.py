"""Routing layer: the error-watcher's main work, between the input and output layers.

`ErrorRouter` pulls a reading from an `ErrorInput`, matches each source's content
against the error pattern, suppresses output it has already alerted on (per
source), and hands any genuinely-new matches to an `ErrorOutput` as one batched
`ErrorAlert`. It depends only on the two layer interfaces, not on tmux or mngr,
so either side can be swapped without touching this core. Dedup state is recorded
only after the output confirms delivery, so an undelivered alert is retried on a
later poll rather than silently dropped.
"""

import re
from collections.abc import Mapping, Sequence
from typing import Final

from loguru import logger

from error_watcher.inputs import ErrorInput
from error_watcher.outputs import ErrorAlert, ErrorOutput

# Single source of truth for the match (REQ-MATCH-1, REQ-MATCH-2, REQ-MATCH-4).
# main() may override this at startup via the ERROR_WATCHER_PATTERN env var, so
# the pattern is threaded into the router rather than read globally.
DEFAULT_ERROR_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"error|exception", re.IGNORECASE
)

# Upper bound on the number of dedup keys retained per source. This is a memory
# ceiling for the permanent process (finding #5): with number-insensitive dedup
# keys (see `dedup_key`) a source rarely accumulates many distinct keys, so this
# is reached only by a source emitting thousands of structurally-distinct error
# lines. When exceeded, arbitrary excess keys are dropped; the worst case is
# re-alerting a previously-seen error, never a crash. Source keys that no longer
# exist are pruned separately (see `prune_seen_sources`).
MAX_SEEN_KEYS_PER_SOURCE: Final[int] = 2048


def match_lines(text: str, pattern: re.Pattern[str]) -> list[str]:
    """Return the lines of `text` that contain a match for `pattern`, in order."""
    return [line for line in text.splitlines() if pattern.search(line)]


def dedup_key(line: str) -> str:
    """Normalize a matched line to the key used for dedup, ignoring volatile numbers.

    Runs of digits (timestamps, counters, numeric request ids) collapse to a
    single '#', so a re-stamped copy of the same error -- '[12:00:05] ERROR x'
    then '[12:00:10] ERROR x' -- shares one key and alerts once rather than on
    every 5s poll (review finding #4). Two errors differing only in their
    numbers are therefore treated as the same for alerting purposes, which
    suits a "something errored" nudge; non-numeric ids (e.g. hex request ids)
    are deliberately left un-normalized to avoid collapsing distinct errors.
    """
    return re.sub(r"\d+", "#", line)


def unseen_matches(
    source: str, current: Sequence[str], seen: Mapping[str, set[str]]
) -> list[str]:
    """Return the matching lines for `source` not already alerted on (read-only).

    `seen` maps source name -> set of dedup keys (see `dedup_key`) already
    alerted on. A line whose key is present is suppressed; every other line is
    returned at most once (lines sharing a key within a single capture collapse
    to the first). This does NOT mutate `seen`: a line is only recorded as
    alerted once an alert is actually sent (see `mark_alerted`), so an error
    whose alert could not be delivered is reconsidered on the next poll rather
    than silently dropped (REQ-MATCH-3).
    """
    already_alerted = seen.get(source, frozenset())
    fresh_lines: list[str] = []
    emitted_keys: set[str] = set()
    for line in current:
        key = dedup_key(line)
        if key in already_alerted or key in emitted_keys:
            continue
        emitted_keys.add(key)
        fresh_lines.append(line)
    return fresh_lines


def mark_alerted(
    matches_by_source: Mapping[str, Sequence[str]], seen: dict[str, set[str]]
) -> None:
    """Record the dedup key of every line in a just-alerted batch so it is not re-alerted.

    Called only after an alert is actually dispatched, so that an undelivered
    alert (no messageable agent, enumeration failure, or a failed send) leaves
    `seen` untouched and the error is retried on a later poll.
    """
    for source, lines in matches_by_source.items():
        already_alerted = seen.setdefault(source, set())
        already_alerted.update(dedup_key(line) for line in lines)
        while len(already_alerted) > MAX_SEEN_KEYS_PER_SOURCE:
            already_alerted.pop()


def prune_seen_sources(seen: dict[str, set[str]], live_sources: Sequence[str]) -> None:
    """Drop dedup state for sources that no longer exist, bounding `seen` growth.

    In a permanent process sources are created and destroyed over time; without
    eviction their dedup sets would accumulate forever (finding #5). Keys absent
    from `live_sources` are removed. The caller must skip this when source
    enumeration failed (an empty list there would wrongly wipe all state), since
    a real session always has at least the watcher's own window.
    """
    for gone in [source for source in seen if source not in set(live_sources)]:
        del seen[gone]


def compile_error_pattern(override: str | None) -> re.Pattern[str]:
    """Compile the match pattern, honoring an optional override (REQ-MATCH-4).

    Falls back to DEFAULT_ERROR_PATTERN when `override` is empty or not a valid
    regular expression, warning rather than crashing on a bad override.
    """
    if not override:
        return DEFAULT_ERROR_PATTERN
    try:
        return re.compile(override, re.IGNORECASE)
    except re.error as e:
        logger.warning("Ignoring invalid ERROR_WATCHER_PATTERN {!r}: {}", override, e)
        return DEFAULT_ERROR_PATTERN


class ErrorRouter:
    """Routes newly-matched error lines from an `ErrorInput` to an `ErrorOutput`."""

    def __init__(
        self,
        error_input: ErrorInput,
        error_output: ErrorOutput,
        pattern: re.Pattern[str],
    ) -> None:
        self._input = error_input
        self._output = error_output
        self._pattern = pattern
        # Maps source name -> set of dedup keys already alerted on, kept across
        # polls for the lifetime of the process.
        self._seen: dict[str, set[str]] = {}

    def run_once(self) -> str | None:
        """Read every source once and, on new matches, deliver one batched alert.

        Returns the delivery id (e.g. the alerted recipient), or None when
        nothing new matched or the alert was not delivered. All I/O goes through
        the input/output layers, whose runners never raise, so a single source's
        failure is logged and skipped without crashing the loop.
        """
        reading = self._input.read()
        if not reading.origin:
            return None
        source_names = [source.name for source in reading.sources]
        # Forget dedup state for sources that have since closed, so `seen` does
        # not grow without bound in this permanent process (finding #5). Skipped
        # when the read came back empty, which signals an input failure rather
        # than a genuinely source-less origin (the watcher's own window always
        # exists), and pruning against an empty list would wipe all state.
        if reading.sources:
            prune_seen_sources(self._seen, source_names)
        matches_by_source: dict[str, list[str]] = {}
        for source in reading.sources:
            matched_lines = match_lines(source.content, self._pattern)
            if not matched_lines:
                continue
            fresh_lines = unseen_matches(source.name, matched_lines, self._seen)
            if fresh_lines:
                matches_by_source[source.name] = fresh_lines
        if not matches_by_source:
            return None
        recipient = self._output.deliver(
            ErrorAlert(origin=reading.origin, matches_by_source=matches_by_source)
        )
        # Only record these lines as alerted once the alert was actually
        # delivered, so an undelivered alert (no messageable agent / failed send)
        # is retried on a later poll instead of being silently dropped
        # (REQ-MATCH-3).
        if recipient is not None:
            mark_alerted(matches_by_source, self._seen)
        return recipient
