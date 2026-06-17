"""Window error watcher service entry point.

Wires the three layers together and runs the poll loop until the bootstrap
manager stops the service:

- input: `TmuxWindowErrorInput` reads every window of the watcher's own tmux
  session (except its own) and returns their current content.
- routing: `ErrorRouter` matches `/error|exception/i`, suppresses already-alerted
  output, and forwards genuinely-new matches as one batched alert.
- output: `RandomMngrAgentErrorOutput` sends the alert to a randomly chosen
  messageable mngr agent.

The layers are swappable here without touching the routing core: substitute a
different `ErrorInput` (e.g. a systemd/journald reader) or a policy-based
`ErrorOutput` (e.g. one that routes to the agent best placed to fix the error).
"""

import os
import random
import signal
import sys
import time
from typing import Final

from loguru import logger

from error_watcher.commands import default_command_runner
from error_watcher.inputs import TmuxWindowErrorInput
from error_watcher.outputs import RandomMngrAgentErrorOutput
from error_watcher.routing import ErrorRouter, compile_error_pattern

# Poll cadence, matching the bootstrap service manager's interval (REQ-SPAWN-2).
POLL_INTERVAL_SECONDS: Final[int] = 5

# The watcher's own service window, skipped while scanning so its alert text
# (which contains "error") does not re-trigger a match (REQ-SCAN-2). The
# bootstrap manager names each service's window svc-<service-name>, so this
# MUST stay in sync with the [services.error-watcher] key in services.toml --
# renaming the service there without updating this constant would silently
# re-enable the feedback loop.
OWN_WINDOW: Final[str] = "svc-error-watcher"


def _handle_signal(signum: int, frame: object) -> None:
    """Exit cleanly on a stop signal so the poll loop terminates (REQ-SPAWN-2)."""
    sys.exit(0)


def main() -> None:
    """Run the poll loop until terminated, alerting on newly-detected errors."""
    logger.info("Starting error watcher (polling every {}s)", POLL_INTERVAL_SECONDS)
    pattern = compile_error_pattern(os.environ.get("ERROR_WATCHER_PATTERN"))
    error_input = TmuxWindowErrorInput(default_command_runner, OWN_WINDOW)
    error_output = RandomMngrAgentErrorOutput(default_command_runner, random.Random())
    router = ErrorRouter(error_input, error_output, pattern)

    # SIGHUP is the signal the bootstrap manager actually delivers when it stops
    # a service (via `tmux kill-window`); SIGTERM/SIGINT are handled too so a
    # manual stop also exits cleanly (REQ-SPAWN-2).
    signal.signal(signal.SIGHUP, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    while True:
        router.run_once()
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
