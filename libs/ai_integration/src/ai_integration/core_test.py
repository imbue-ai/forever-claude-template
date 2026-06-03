import functools

import anyio
import pytest

from ai_integration.core import run_completion
from ai_integration.data_types import BillingPath, CompletionResult, Usage
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
            service_name="svc",
            env={"ANTHROPIC_API_KEY": "sk"},
            api_backend=fake_api,
            cli_backend=fake_cli,
        )
    )
    assert result.billing_path is BillingPath.DIRECT_API
    assert seen["api_key"] == "sk"


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
            service_name="svc",
            env=env,
            api_backend=fake_api,
            cli_backend=fake_cli,
        )
    )
    assert result.billing_path is BillingPath.CLAUDE_CLI
    # The env handed to the cli backend must have MAIN_CLAUDE_SESSION_ID stripped.
    cli_env = seen["env"]
    assert isinstance(cli_env, dict)
    assert "MAIN_CLAUDE_SESSION_ID" not in cli_env


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
                service_name="svc",
                env=env,
                api_backend=fake_api,
                cli_backend=fake_cli,
            )
        )


def test_run_completion_records_spend(tmp_path) -> None:
    tracker = SpendTracker(
        "svc",
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
            service_name="svc",
            env={"ANTHROPIC_API_KEY": "sk"},
            spend_tracker=tracker,
            api_backend=fake_api,
            cli_backend=fake_cli,
        )
    )
    assert tracker.spent_in_window() == 0.001
