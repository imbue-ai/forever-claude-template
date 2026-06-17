"""Unit tests for the subprocess command runner (the watcher's only I/O seam)."""

from error_watcher.commands import (
    RUNNER_FAILURE_RETURNCODE,
    default_command_runner,
)


def test_default_command_runner_returns_stdout_for_a_successful_command() -> None:
    result = default_command_runner(["printf", "hello"])
    assert result.returncode == 0
    assert result.stdout == "hello"


def test_default_command_runner_never_raises_on_a_missing_binary() -> None:
    # A spawn failure (binary not found) must surface as the runner-failure
    # sentinel, never an exception, so the poll loop cannot crash (REQ-SPAWN-4).
    result = default_command_runner(["this-binary-does-not-exist-error-watcher"])
    assert result.returncode == RUNNER_FAILURE_RETURNCODE
    assert result.stdout == ""
    assert result.stderr != ""


def test_default_command_runner_times_out_with_the_failure_sentinel() -> None:
    # A hung command must time out into the sentinel rather than wedging the loop.
    result = default_command_runner(["sleep", "5"], timeout=0.05)
    assert result.returncode == RUNNER_FAILURE_RETURNCODE
    assert result.returncode != 1  # distinct from a real exit-1


def test_default_command_runner_preserves_partial_output_on_timeout() -> None:
    # A command that prints before it hangs must have that partial stdout
    # preserved (finding #6), not discarded -- subprocess hands back the buffered
    # output as bytes on the timeout path even under text=True, so the runner has
    # to decode it. `printf` writes immediately and the sleep far outlasts the
    # timeout, so the marker is buffered before the timeout fires.
    result = default_command_runner(
        ["sh", "-c", "printf partial-payload; sleep 5"], timeout=0.2
    )
    assert result.returncode == RUNNER_FAILURE_RETURNCODE
    assert result.stdout == "partial-payload"


def test_default_command_runner_failure_sentinel_is_not_a_real_exit_code() -> None:
    # Guards the contract that the sentinel cannot collide with a process exit
    # status (which is always >= 0).
    assert RUNNER_FAILURE_RETURNCODE < 0
