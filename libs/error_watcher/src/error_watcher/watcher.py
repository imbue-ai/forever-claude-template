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

# mngr refuses to message an agent in this lifecycle state (REQ-NOTIFY-3).
STOPPED_STATE: Final[str] = "STOPPED"

# Hard timeout for any single tmux/mngr invocation so a hung command cannot
# wedge the poll loop.
_COMMAND_TIMEOUT_SECONDS: Final[float] = 30.0


class AgentSummary(NamedTuple):
    """One agent from `mngr list --format json`, reduced to the fields we need.

    `state` is the agent's lifecycle state string (e.g. RUNNING, WAITING,
    STOPPED); the messageable filter keys off it.
    """

    name: str
    state: str


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


def unseen_matches(
    window: str, current: Sequence[str], seen: Mapping[str, set[str]]
) -> list[str]:
    """Return the matching lines for `window` not already alerted on (read-only).

    `seen` maps window name -> set of lines already alerted on. A line present
    in `seen[window]` is suppressed; every other line is returned at most once
    (duplicates within a single capture collapse to one). This does NOT mutate
    `seen`: a line is only recorded as alerted once an alert is actually sent
    (see `mark_alerted`), so an error whose alert could not be delivered is
    reconsidered on the next poll rather than silently dropped (REQ-MATCH-3).
    """
    already_alerted = seen.get(window, frozenset())
    fresh_lines: list[str] = []
    emitted: set[str] = set()
    for line in current:
        if line in already_alerted or line in emitted:
            continue
        emitted.add(line)
        fresh_lines.append(line)
    return fresh_lines


def mark_alerted(
    matches_by_window: Mapping[str, Sequence[str]], seen: dict[str, set[str]]
) -> None:
    """Record every line in a just-alerted batch as seen so it is not re-alerted.

    Called only after an alert is actually dispatched, so that an undelivered
    alert (no messageable agent, enumeration failure, or a failed send) leaves
    `seen` untouched and the error is retried on a later poll.
    """
    for window, lines in matches_by_window.items():
        already_alerted = seen.setdefault(window, set())
        already_alerted.update(lines)


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

    The CLI emits `{"agents": [{"name": ..., "state": ..., ...}], "errors": [...]}`.
    Tolerant by design (REQ-SPAWN-4): malformed or unexpected output yields an
    empty list plus a warning so the poll loop never crashes. Agents missing a
    usable name or state are skipped, since the messageable filter needs both.
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
        if isinstance(name, str) and name and isinstance(state, str) and state:
            summaries.append(AgentSummary(name=name, state=state))
    return summaries


def choose_recipient(names: Sequence[str], rng: random.Random) -> str | None:
    """Return a uniformly random name, or None if `names` is empty (REQ-NOTIFY-5)."""
    if not names:
        return None
    return rng.choice(list(names))


def select_messageable_names(agents: Sequence[AgentSummary]) -> list[str]:
    """Return the names of agents that can currently receive a message.

    mngr refuses to message a STOPPED agent, so STOPPED agents are excluded
    (REQ-NOTIFY-3); every other lifecycle state is treated as messageable. The
    watcher never starts a stopped agent just to alert it.
    """
    return [agent.name for agent in agents if agent.state != STOPPED_STATE]


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


def _default_command_runner(command: Sequence[str]) -> CommandResult:
    try:
        completed = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            timeout=_COMMAND_TIMEOUT_SECONDS,
        )
    except (subprocess.SubprocessError, OSError) as e:
        # Never raise: a hung or missing tmux/mngr must surface as a non-zero
        # result the caller logs and skips, not a crash of the poll loop.
        return CommandResult(returncode=1, stdout="", stderr=str(e))
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
    """Enumerate messageable agents and send `message` to one chosen at random.

    Returns the recipient only when the message was actually delivered. Returns
    None when enumeration failed, no agent is messageable (REQ-NOTIFY-4), or the
    send itself failed -- so the caller does not record the error as alerted and
    retries it on a later poll. A failed send is logged, not raised (REQ-SPAWN-4).
    """
    list_result = run(build_list_command())
    if list_result.returncode != 0:
        logger.warning(
            "Could not enumerate agents to alert: {}", list_result.stderr.strip()
        )
        return None
    messageable_names = select_messageable_names(
        parse_agent_summaries(list_result.stdout)
    )
    recipient = choose_recipient(messageable_names, rng)
    if recipient is None:
        logger.warning(
            "Detected new error output but found no messageable agent to alert"
        )
        return None
    send_result = run(build_message_command(recipient, message))
    if send_result.returncode != 0:
        logger.warning(
            "Failed to alert agent {}: {}", recipient, send_result.stderr.strip()
        )
        return None
    logger.info("Alerted agent {} about new error output", recipient)
    return recipient


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
    matches_by_window: dict[str, list[str]] = {}
    for window in list_windows(run, session):
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


def main() -> None:
    """Run the poll loop until terminated, alerting on newly-detected errors."""
    logger.info("Starting error watcher (polling every {}s)", POLL_INTERVAL_SECONDS)
    runner: CommandRunner = _default_command_runner
    pattern = compile_error_pattern(os.environ.get("ERROR_WATCHER_PATTERN"))
    seen: dict[str, set[str]] = {}
    rng = random.Random()

    def _handle_signal(signum: int, frame: object) -> None:
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    while True:
        run_one_poll(runner, seen, rng, pattern)
        time.sleep(POLL_INTERVAL_SECONDS)
