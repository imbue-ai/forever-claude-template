"""The single subprocess seam shared by the error-watcher's input and output layers.

Every tmux/mngr invocation in the input and output layers runs through a
`CommandRunner`. The production runner (`default_command_runner`) never raises:
a missing binary, a timeout, or any OS error is reported as a `CommandResult`
carrying `RUNNER_FAILURE_RETURNCODE`, so a single failed command can never crash
the poll loop. Tests inject a fake runner to exercise a full poll without real
tmux or mngr.
"""

import subprocess
from collections.abc import Callable, Sequence
from typing import Final, NamedTuple


class CommandResult(NamedTuple):
    """The outcome of running a single tmux or mngr command."""

    returncode: int
    stdout: str
    stderr: str


# Runs an argv and returns its outcome. The production layers use the
# subprocess-backed default; tests inject a fake so a full poll is exercised
# without real tmux/mngr.
CommandRunner = Callable[[Sequence[str]], CommandResult]

# Hard timeout for any single tmux/mngr invocation so a hung command cannot
# wedge the poll loop.
_COMMAND_TIMEOUT_SECONDS: Final[float] = 30.0

# Synthetic returncode used when the command could not be run at all (missing
# binary, timeout, OS error) -- i.e. no real process exit code exists. Kept
# distinct from any real exit status (which are >= 0) so a runner-level failure
# is never confused with a command that genuinely exited 1.
RUNNER_FAILURE_RETURNCODE: Final[int] = -1


def _decode_timeout_output(output: str | bytes | None) -> str:
    """Normalize a TimeoutExpired stdout/stderr payload to str.

    On a timeout, subprocess attaches the buffered output as bytes even when the
    process was started with text=True (it skips the decode it would do on a
    normal return), so this accepts str, bytes, or None. Bytes are decoded with
    errors="replace" so a multibyte character truncated at the timeout boundary
    cannot raise UnicodeDecodeError into the never-raise runner.
    """
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode(errors="replace")
    return output


def default_command_runner(
    command: Sequence[str], timeout: float = _COMMAND_TIMEOUT_SECONDS
) -> CommandResult:
    """Run `command` via subprocess, reporting any failure as a CommandResult (never raises)."""
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
        # use a partial payload is not robbed of it (finding #6). On the timeout
        # path subprocess hands back the buffered output as bytes even under
        # text=True (it skips the usual decode when the timeout kills the
        # process), so decode it ourselves rather than dropping a bytes payload.
        partial_stdout = _decode_timeout_output(e.stdout)
        partial_stderr = _decode_timeout_output(e.stderr)
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
