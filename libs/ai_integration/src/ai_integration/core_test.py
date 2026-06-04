import functools
import subprocess
from pathlib import Path

import anyio
import pytest

from ai_integration.core import (
    _run_create_worker_blocking,
    run_agent,
    run_completion,
    run_task,
)
from ai_integration.data_types import AgentOutcome, BillingPath, CompletionResult, Usage
from ai_integration.errors import CredentialsUnavailableError
from ai_integration.spend import SpendTracker


def _result(billing: BillingPath) -> CompletionResult:
    return CompletionResult(
        text="ok",
        billing_path=billing,
        model="claude-haiku-4-5",
        usage=Usage(input_tokens=1, output_tokens=1),
        cost_usd=0.001,
    )


def test_run_completion_prefers_direct_api_when_key_present() -> None:
    seen: dict[str, object] = {}

    async def fake_api(**kwargs: object) -> CompletionResult:
        seen.update(kwargs)
        return _result(BillingPath.DIRECT_API)

    async def fake_cli(**kwargs: object) -> CompletionResult:
        raise AssertionError("cli backend must not run when a key is present")

    result = anyio.run(
        functools.partial(
            run_completion,
            "hello",
            system="You are terse.",
            service_name="svc",
            env={"ANTHROPIC_API_KEY": "sk"},
            api_backend=fake_api,
            cli_backend=fake_cli,
            spend_loader=lambda _name: None,
        )
    )
    assert result.billing_path is BillingPath.DIRECT_API
    assert seen["api_key"] == "sk"
    # The required system prompt flows to the API backend as the system block.
    assert seen["system"] == "You are terse."


def test_run_completion_falls_back_to_cli_without_key(tmp_path) -> None:
    (tmp_path / ".credentials.json").write_text("{}")
    env = {
        "CLAUDE_CONFIG_DIR": str(tmp_path),
        "HOME": str(tmp_path / "home"),
        "MAIN_CLAUDE_SESSION_ID": "sess",
    }
    seen: dict[str, object] = {}

    async def fake_api(**kwargs: object) -> CompletionResult:
        raise AssertionError("api backend must not run without a key")

    async def fake_cli(**kwargs: object) -> CompletionResult:
        seen.update(kwargs)
        return _result(BillingPath.CLAUDE_CLI)

    result = anyio.run(
        functools.partial(
            run_completion,
            "hi",
            system="You are terse.",
            service_name="svc",
            env=env,
            api_backend=fake_api,
            cli_backend=fake_cli,
            spend_loader=lambda _name: None,
        )
    )
    assert result.billing_path is BillingPath.CLAUDE_CLI
    # The env handed to the cli backend must have MAIN_CLAUDE_SESSION_ID stripped.
    cli_env = seen["env"]
    assert isinstance(cli_env, dict)
    assert "MAIN_CLAUDE_SESSION_ID" not in cli_env
    # The lean non-agentic fallback passes the system prompt and disables tools,
    # so the call answers the prompt rather than being hijacked by CLAUDE.md.
    assert seen["system"] == "You are terse."
    assert seen["tools"] == ""


def test_run_completion_raises_without_any_credentials(tmp_path) -> None:
    env = {"HOME": str(tmp_path)}  # no key, no credential files

    async def fake_api(**kwargs: object) -> CompletionResult:
        raise AssertionError

    async def fake_cli(**kwargs: object) -> CompletionResult:
        raise AssertionError

    with pytest.raises(CredentialsUnavailableError):
        anyio.run(
            functools.partial(
                run_completion,
                "hi",
                system="You are terse.",
                service_name="svc",
                env=env,
                api_backend=fake_api,
                cli_backend=fake_cli,
                spend_loader=lambda _name: None,
            )
        )


def test_run_completion_records_spend(tmp_path) -> None:
    tracker = SpendTracker(
        service_name="svc",
        ceiling_usd=10.0,
        state_root=tmp_path,
        window_seconds=1000,
        clock=lambda: 1000.0,
    )

    async def fake_api(**kwargs: object) -> CompletionResult:
        return _result(BillingPath.DIRECT_API)

    async def fake_cli(**kwargs: object) -> CompletionResult:
        raise AssertionError

    anyio.run(
        functools.partial(
            run_completion,
            "hi",
            system="You are terse.",
            service_name="svc",
            env={"ANTHROPIC_API_KEY": "sk"},
            spend_loader=lambda _name: tracker,
            api_backend=fake_api,
            cli_backend=fake_cli,
        )
    )
    assert tracker.spent_in_window() == 0.001


def test_run_task_forwards_append_system_and_keeps_tools_enabled(tmp_path) -> None:
    (tmp_path / ".credentials.json").write_text("{}")
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path), "HOME": str(tmp_path / "home")}
    seen: dict[str, object] = {}

    async def fake_cli(**kwargs: object) -> CompletionResult:
        seen.update(kwargs)
        return _result(BillingPath.CLAUDE_CLI)

    anyio.run(
        functools.partial(
            run_task,
            "do the work",
            service_name="svc",
            env=env,
            append_system="Extra task instructions.",
            cli_backend=fake_cli,
            spend_loader=lambda _name: None,
        )
    )
    assert seen["append_system"] == "Extra task instructions."
    # Agentic tasks ride on the default agent: tools must NOT be disabled, and
    # no replacement system prompt is forced.
    assert "tools" not in seen
    assert seen["system"] is None


@pytest.mark.parametrize(
    "payload,expected_outcome",
    [
        ({"name": "done", "type": "status"}, AgentOutcome.DONE),
        ({"name": "stuck", "type": "status"}, AgentOutcome.STUCK),
        ({"name": "no-update-needed", "type": "status"}, AgentOutcome.NO_UPDATE_NEEDED),
        ({"name": "something-else", "type": "gate"}, AgentOutcome.UNKNOWN),
        ({"timed_out": True}, AgentOutcome.TIMED_OUT),
    ],
)
def test_run_agent_maps_report_name_to_outcome(
    payload: dict[str, object], expected_outcome: AgentOutcome
) -> None:
    captured: dict[str, object] = {}

    def fake_runner(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {**payload, "branch": "mngr/demo", "body": "hi", "raw_report": "raw"}

    result = anyio.run(
        functools.partial(
            run_agent,
            name="demo",
            template="worker",
            runtime_dir=Path("/tmp/runtime"),
            task_file=Path("/tmp/runtime/task.md"),
            service_name="svc",
            repo_root=Path("/repo"),
            runner=fake_runner,
        )
    )
    assert result.outcome is expected_outcome
    assert result.branch == "mngr/demo"
    # The injected runner is handed the resolved repo_root, not the process cwd.
    assert captured["repo_root"] == Path("/repo")


def test_create_worker_subprocess_runs_in_repo_root(tmp_path) -> None:
    """The ``uv run create_worker`` subprocess must execute with ``cwd=repo_root``.

    ``uv run`` resolves its project from the working directory, not the script
    path, so a ``repo_root`` that differs from the process cwd is only honored if
    cwd is set explicitly. Regression test for that.
    """
    seen_argv: list[str] = []
    seen_cwd: list[object] = []
    repo_root = tmp_path / "checkout"

    def fake_subprocess_run(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        seen_argv.extend(argv)
        seen_cwd.append(kwargs.get("cwd"))
        # create_worker writes the result JSON to the ``--result-json`` path; mimic
        # that so the blocking helper can read a collected report back.
        result_path = Path(argv[argv.index("--result-json") + 1])
        result_path.write_text(
            '{"timed_out": false, "name": "done", "branch": "mngr/x"}'
        )
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    payload = _run_create_worker_blocking(
        name="x",
        template="worker",
        runtime_dir=tmp_path / "runtime",
        task_file=tmp_path / "runtime" / "task.md",
        timeout="30m",
        poll_interval="5s",
        keep_agent=False,
        repo_root=repo_root,
        subprocess_run=fake_subprocess_run,
    )
    assert seen_cwd == [str(repo_root)]
    # The script path is also anchored to repo_root, matching the cwd.
    assert str(repo_root) in seen_argv[2]
    assert payload["name"] == "done"
