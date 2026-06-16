"""Window error watcher service.

Scans every tmux window in the session for output matching /error|exception/i
and, on newly-appeared matches, sends one batched message to a randomly
selected mngr agent. The polling loop and tmux/mngr I/O are wired up in main();
the functions below are the pure, side-effect-free core (matching, dedup, alert
formatting, mngr argv assembly, agent parsing, and recipient selection).
"""

import json
import os
import random
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Final, NamedTuple

from loguru import logger

# Single source of truth for the match (REQ-MATCH-1, REQ-MATCH-2, REQ-MATCH-4).
# main() may override this at startup via the ERROR_WATCHER_PATTERN env var, so
# the pattern is threaded into match_lines() rather than read globally.
DEFAULT_ERROR_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"error|exception", re.IGNORECASE
)

# Each matching line is truncated to this length in the alert so a single giant
# traceback line cannot blow up the message sent to the agent.
MAX_ALERT_LINE_LENGTH: Final[int] = 500

# Poll cadence, matching the bootstrap service manager's interval (REQ-SPAWN-2).
POLL_INTERVAL_SECONDS: Final[int] = 5

# The watcher's own service window, skipped while scanning so its alert text
# (which contains "error") does not re-trigger a match (REQ-SCAN-2). The
# bootstrap manager names each service's window svc-<service-name>, so this
# MUST stay in sync with the [services.error-watcher] key in services.toml --
# renaming the service there without updating this constant would silently
# re-enable the feedback loop.
OWN_WINDOW: Final[str] = "svc-error-watcher"

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

# Hard timeout for any single tmux/mngr invocation so a hung command cannot
# wedge the poll loop.
_COMMAND_TIMEOUT_SECONDS: Final[float] = 30.0

# Synthetic returncode used when the command could not be run at all (missing
# binary, timeout, OS error) -- i.e. no real process exit code exists. Kept
# distinct from any real exit status (which are >= 0) so a runner-level failure
# is never confused with a command that genuinely exited 1.
RUNNER_FAILURE_RETURNCODE: Final[int] = -1

# Upper bound on the number of dedup keys retained per window. This is a memory
# ceiling for the permanent process (finding #5): with number-insensitive dedup
# keys (see `dedup_key`) a window rarely accumulates many distinct keys, so this
# is reached only by a window emitting thousands of structurally-distinct error
# lines. When exceeded, arbitrary excess keys are dropped; the worst case is
# re-alerting a previously-seen error, never a crash. Window keys that no longer
# exist are pruned separately (see `prune_seen_windows`).
MAX_SEEN_KEYS_PER_WINDOW: Final[int] = 2048


class AgentSummary(NamedTuple):
    """One agent from `mngr list --format json`, reduced to the fields we need.

    `state` is the agent's lifecycle state string (e.g. RUNNING, WAITING,
    STOPPED) and `agent_type` is its type (e.g. claude, main); the messageable
    filter keys off both.
    """

    name: str
    state: str
    agent_type: str


class CommandResult(NamedTuple):
    """The outcome of running a single tmux or mngr command."""

    returncode: int
    stdout: str
    stderr: str


# Runs an argv and returns its outcome. main() uses the subprocess-backed
# default; tests inject a fake so a full poll is exercised without real tmux.
CommandRunner = Callable[[Sequence[str]], CommandResult]


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
    window: str, current: Sequence[str], seen: Mapping[str, set[str]]
) -> list[str]:
    """Return the matching lines for `window` not already alerted on (read-only).

    `seen` maps window name -> set of dedup keys (see `dedup_key`) already
    alerted on. A line whose key is present is suppressed; every other line is
    returned at most once (lines sharing a key within a single capture collapse
    to the first). This does NOT mutate `seen`: a line is only recorded as
    alerted once an alert is actually sent (see `mark_alerted`), so an error
    whose alert could not be delivered is reconsidered on the next poll rather
    than silently dropped (REQ-MATCH-3).
    """
    already_alerted = seen.get(window, frozenset())
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
    matches_by_window: Mapping[str, Sequence[str]], seen: dict[str, set[str]]
) -> None:
    """Record the dedup key of every line in a just-alerted batch so it is not re-alerted.

    Called only after an alert is actually dispatched, so that an undelivered
    alert (no messageable agent, enumeration failure, or a failed send) leaves
    `seen` untouched and the error is retried on a later poll.
    """
    for window, lines in matches_by_window.items():
        already_alerted = seen.setdefault(window, set())
        already_alerted.update(dedup_key(line) for line in lines)
        while len(already_alerted) > MAX_SEEN_KEYS_PER_WINDOW:
            already_alerted.pop()


def prune_seen_windows(seen: dict[str, set[str]], live_windows: Sequence[str]) -> None:
    """Drop dedup state for windows that no longer exist, bounding `seen` growth.

    In a permanent process windows are created and destroyed over time; without
    eviction their dedup sets would accumulate forever (finding #5). Keys absent
    from `live_windows` are removed. The caller must skip this when window
    enumeration failed (an empty list there would wrongly wipe all state), since
    a real session always has at least the watcher's own window.
    """
    for gone in [window for window in seen if window not in set(live_windows)]:
        del seen[gone]


def _truncate_line(line: str) -> str:
    if len(line) <= MAX_ALERT_LINE_LENGTH:
        return line
    return line[:MAX_ALERT_LINE_LENGTH] + "..."


def format_alert(session: str, matches_by_window: Mapping[str, Sequence[str]]) -> str:
    """Build one human-readable alert covering every window that newly matched this poll.

    A single message names each window and includes its matching line(s), so
    multiple windows erroring in one poll yield one batched message rather than
    one per window (REQ-NOTIFY-2, REQ-NOTIFY-6).
    """
    header = (
        f"Possible error/exception detected by error-watcher in session '{session}':"
    )
    window_lines = [
        f"- window '{window}': {' | '.join(_truncate_line(line) for line in lines)}"
        for window, lines in matches_by_window.items()
    ]
    return "\n".join([header, *window_lines])


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
            summaries.append(
                AgentSummary(name=name, state=state, agent_type=type_str)
            )
    return summaries


def choose_recipient(names: Sequence[str], rng: random.Random) -> str | None:
    """Return a uniformly random name, or None if `names` is empty (REQ-NOTIFY-5)."""
    if not names:
        return None
    return rng.choice(list(names))


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


def _default_command_runner(
    command: Sequence[str], timeout: float = _COMMAND_TIMEOUT_SECONDS
) -> CommandResult:
    try:
        completed = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        # Never raise: a hung tmux/mngr must surface as a runner failure the
        # caller logs and skips, not a crash of the poll loop. Preserve whatever
        # the command managed to emit before the timeout so a caller that can
        # use a partial payload is not robbed of it (finding #6).
        partial_stdout = e.stdout if isinstance(e.stdout, str) else ""
        partial_stderr = e.stderr if isinstance(e.stderr, str) else ""
        return CommandResult(
            returncode=RUNNER_FAILURE_RETURNCODE,
            stdout=partial_stdout,
            stderr=partial_stderr or f"timed out after {timeout}s",
        )
    except (subprocess.SubprocessError, OSError) as e:
        # Missing binary or other spawn failure: no exit code exists, so use the
        # synthetic runner-failure sentinel rather than colliding with exit 1.
        return CommandResult(
            returncode=RUNNER_FAILURE_RETURNCODE, stdout="", stderr=str(e)
        )
    return CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def get_session_name(run: CommandRunner) -> str:
    """Return the watcher's own tmux session name, or "" if it cannot be determined."""
    result = run(["tmux", "display-message", "-p", "#S"])
    if result.returncode != 0:
        logger.warning(
            "Could not determine tmux session name: {}", result.stderr.strip()
        )
        return ""
    return result.stdout.strip()


def list_windows(run: CommandRunner, session: str) -> list[str]:
    """Return every window name in `session` (empty list on failure, REQ-SCAN-1)."""
    result = run(["tmux", "list-windows", "-t", session, "-F", "#{window_name}"])
    if result.returncode != 0:
        logger.warning(
            "Could not list windows for session {}: {}", session, result.stderr.strip()
        )
        return []
    return [line for line in result.stdout.splitlines() if line]


def capture_window(run: CommandRunner, session: str, window: str) -> str:
    """Return the visible pane text of `window` ("" if it could not be captured).

    A window can be destroyed between enumeration and capture; that is tolerated
    by returning empty text rather than raising (REQ-SCAN-3).
    """
    result = run(["tmux", "capture-pane", "-t", f"{session}:{window}", "-p"])
    if result.returncode != 0:
        logger.debug(
            "Could not capture window {} (it may have closed): {}",
            window,
            result.stderr.strip(),
        )
        return ""
    return result.stdout


def _alert_random_agent(
    run: CommandRunner, message: str, rng: random.Random
) -> str | None:
    """Enumerate messageable agents and send `message` to one, with fallback.

    Recipients are tried in uniformly random order (REQ-NOTIFY-5): the first pick
    is uniform across the pool, and on a failed send we fall back to the next
    agent so one bad pick -- e.g. an agent that stopped between `mngr list` and
    `mngr message` -- does not drop the alert while other agents are still
    reachable. Returns the recipient that actually received the message, or None
    when enumeration failed, no agent is messageable (REQ-NOTIFY-4), or every
    candidate's send failed -- so the caller does not record the error as alerted
    and retries it on a later poll. A failed send is logged, not raised
    (REQ-SPAWN-4).
    """
    list_result = run(build_list_command())
    # Parse the payload regardless of exit status: mngr can exit non-zero (e.g.
    # one provider failed) while still emitting a valid {"agents": [...]} body,
    # and dropping that would needlessly skip the alert (finding #6). Only treat
    # a non-zero exit as fatal when it left us with no usable agents.
    agents = parse_agent_summaries(list_result.stdout)
    if list_result.returncode != 0 and not agents:
        logger.warning(
            "Could not enumerate agents to alert: {}", list_result.stderr.strip()
        )
        return None
    remaining = select_messageable_names(agents)
    if not remaining:
        logger.warning(
            "Detected new error output but found no messageable agent to alert"
        )
        return None
    while remaining:
        recipient = choose_recipient(remaining, rng)
        if recipient is None:
            break
        remaining.remove(recipient)
        send_result = run(build_message_command(recipient, message))
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


def run_one_poll(
    run: CommandRunner,
    seen: dict[str, set[str]],
    rng: random.Random,
    pattern: re.Pattern[str],
) -> str | None:
    """Scan every window once and, on new matches, alert one random agent.

    Returns the alerted recipient, or None when nothing new matched or no alert
    was sent. Every tmux/mngr call goes through `run`, which never raises, so a
    single window's failure is logged and skipped without crashing the loop
    (REQ-SPAWN-4, REQ-SCAN-3).
    """
    session = get_session_name(run)
    if not session:
        return None
    windows = list_windows(run, session)
    # Forget dedup state for windows that have since closed, so `seen` does not
    # grow without bound in this permanent process (finding #5). Skipped when
    # enumeration came back empty, which signals a failed list rather than a
    # genuinely window-less session (the watcher's own window always exists).
    if windows:
        prune_seen_windows(seen, windows)
    matches_by_window: dict[str, list[str]] = {}
    for window in windows:
        if window == OWN_WINDOW:
            continue
        matched_lines = match_lines(capture_window(run, session, window), pattern)
        if not matched_lines:
            continue
        fresh_lines = unseen_matches(window, matched_lines, seen)
        if fresh_lines:
            matches_by_window[window] = fresh_lines
    if not matches_by_window:
        return None
    recipient = _alert_random_agent(run, format_alert(session, matches_by_window), rng)
    # Only record these lines as alerted once the message was actually
    # delivered, so an undelivered alert (no messageable agent / failed send)
    # is retried on a later poll instead of being silently dropped (REQ-MATCH-3).
    if recipient is not None:
        mark_alerted(matches_by_window, seen)
    return recipient


def _handle_signal(signum: int, frame: object) -> None:
    """Exit cleanly on a stop signal so the poll loop terminates (REQ-SPAWN-2)."""
    sys.exit(0)


def main() -> None:
    """Run the poll loop until terminated, alerting on newly-detected errors."""
    logger.info("Starting error watcher (polling every {}s)", POLL_INTERVAL_SECONDS)
    runner: CommandRunner = _default_command_runner
    pattern = compile_error_pattern(os.environ.get("ERROR_WATCHER_PATTERN"))
    seen: dict[str, set[str]] = {}
    rng = random.Random()

    # SIGHUP is the signal the bootstrap manager actually delivers when it stops
    # a service (via `tmux kill-window`); SIGTERM/SIGINT are handled too so a
    # manual stop also exits cleanly (REQ-SPAWN-2).
    signal.signal(signal.SIGHUP, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    while True:
        run_one_poll(runner, seen, rng, pattern)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
