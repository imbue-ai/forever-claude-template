"""The three escalating-agency entry points services call.

``run_completion`` -- pattern 3, no agency: direct Anthropic API when a key is
present (always cheaper for non-agentic work), else the ``claude -p`` fallback.
The routing is implicit (by key presence), so a keyless service works immediately
and adding a key later transparently upgrades it; the keyless path also logs the
calculated savings a key would unlock.

``run_task`` -- pattern 2, one-shot agentic: always ``claude -p`` (it can use
tools / read files, which the plain API call cannot).

``run_agent`` -- pattern 1, full agent: a thin wrapper over the synchronous
``create_worker.py run`` launch -> await -> collect -> destroy path.
"""

import json
import os
import subprocess
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path

from anyio import to_thread
from loguru import logger

from ai_integration import backends
from ai_integration.credentials import (
    build_claude_cli_env,
    get_api_key,
    require_credentials,
)
from ai_integration.data_types import AgentOutcome, AgentResult, CompletionResult
from ai_integration.errors import AgentRunError
from ai_integration.pricing import DEFAULT_MODEL, counterfactual_direct_api_cost_usd
from ai_integration.spend import SpendTracker

_CREATE_WORKER_REL = ".agents/skills/launch-task/scripts/create_worker.py"

# Type aliases for the injectable backends (tests pass fakes).
_ApiBackend = Callable[..., Awaitable[CompletionResult]]
_CliBackend = Callable[..., Awaitable[CompletionResult]]


def _log_keyless_savings(result: CompletionResult, prompt: str, model: str) -> None:
    """Log the calculated savings a direct-API key would unlock for this call.

    Only meaningful on the ``claude -p`` fallback (where ``cost_usd`` is the actual
    reported cost). The counterfactual prices just the user's prompt + response --
    a direct call carries none of ``claude -p``'s agent-context overhead.
    """
    if result.cost_usd is None:
        return
    counterfactual = counterfactual_direct_api_cost_usd(model, prompt, result.text)
    if counterfactual is None or counterfactual >= result.cost_usd:
        return
    logger.info(
        "ai_integration: this claude -p call cost ~${:.4f}; the same call via the "
        "direct Anthropic API would cost ~${:.4f} (estimate). Set ANTHROPIC_API_KEY "
        "to save ~${:.4f} per call.",
        result.cost_usd,
        counterfactual,
        result.cost_usd - counterfactual,
    )


async def run_completion(
    prompt: str,
    *,
    service_name: str,
    model: str = DEFAULT_MODEL,
    system: str | None = None,
    max_tokens: int = 1024,
    spend_tracker: SpendTracker | None = None,
    env: Mapping[str, str] | None = None,
    anthropic_options: Mapping[str, object] | None = None,
    strip_mngr_agent_vars: bool = False,
    claude_cli_args: Sequence[str] | None = None,
    api_backend: _ApiBackend = backends.complete_via_api,
    cli_backend: _CliBackend = backends.complete_via_cli,
) -> CompletionResult:
    """Pattern 3: one non-agentic completion, direct API if keyed else ``claude -p``."""
    resolved_env = os.environ if env is None else env
    if spend_tracker is not None:
        spend_tracker.check_ceiling()

    api_key = get_api_key(resolved_env)
    if api_key is not None:
        result = await api_backend(
            api_key=api_key,
            model=model,
            prompt=prompt,
            system=system,
            max_tokens=max_tokens,
            options=anthropic_options,
        )
    else:
        require_credentials(resolved_env)
        cli_env = build_claude_cli_env(resolved_env, strip_mngr_agent_vars)
        result = await cli_backend(
            model=model, prompt=prompt, env=cli_env, extra_args=claude_cli_args
        )
        _log_keyless_savings(result, prompt, model)

    logger.info(
        "ai_integration completion: service={} model={} billing={} cost_usd={}",
        service_name,
        model,
        result.billing_path.value,
        result.cost_usd,
    )
    if spend_tracker is not None and result.cost_usd is not None:
        spend_tracker.record(result.cost_usd)
    return result


async def run_task(
    prompt: str,
    *,
    service_name: str,
    model: str = DEFAULT_MODEL,
    spend_tracker: SpendTracker | None = None,
    env: Mapping[str, str] | None = None,
    strip_mngr_agent_vars: bool = False,
    claude_cli_args: Sequence[str] | None = None,
    cli_backend: _CliBackend = backends.complete_via_cli,
) -> CompletionResult:
    """Pattern 2: one-shot agentic task via headless ``claude -p`` (tools/file access)."""
    resolved_env = os.environ if env is None else env
    if spend_tracker is not None:
        spend_tracker.check_ceiling()
    require_credentials(resolved_env)
    cli_env = build_claude_cli_env(resolved_env, strip_mngr_agent_vars)
    result = await cli_backend(
        model=model, prompt=prompt, env=cli_env, extra_args=claude_cli_args
    )
    logger.info(
        "ai_integration task: service={} model={} billing={} cost_usd={}",
        service_name,
        model,
        result.billing_path.value,
        result.cost_usd,
    )
    if spend_tracker is not None and result.cost_usd is not None:
        spend_tracker.record(result.cost_usd)
    return result


def _outcome_for(payload: Mapping[str, object]) -> AgentOutcome:
    if payload.get("timed_out"):
        return AgentOutcome.TIMED_OUT
    name = payload.get("name")
    if name == "done":
        return AgentOutcome.DONE
    if name == "stuck":
        return AgentOutcome.STUCK
    if name == "no-update-needed":
        return AgentOutcome.NO_UPDATE_NEEDED
    return AgentOutcome.UNKNOWN


def _agent_result_from_payload(payload: Mapping[str, object]) -> AgentResult:
    report_type = payload.get("type")
    report_name = payload.get("name")
    body = payload.get("body")
    branch = payload.get("branch")
    raw = payload.get("raw_report")
    return AgentResult(
        outcome=_outcome_for(payload),
        report_type=report_type if isinstance(report_type, str) else None,
        report_name=report_name if isinstance(report_name, str) else None,
        body=body if isinstance(body, str) else "",
        branch=branch if isinstance(branch, str) else None,
        raw_report=raw if isinstance(raw, str) else "",
    )


def _run_create_worker_blocking(
    *,
    name: str,
    template: str,
    runtime_dir: Path,
    task_file: Path,
    timeout: str,
    poll_interval: str,
    keep_agent: bool,
    repo_root: Path,
) -> Mapping[str, object]:
    argv = [
        "uv",
        "run",
        str(repo_root / _CREATE_WORKER_REL),
        "run",
        "--name",
        name,
        "--template",
        template,
        "--runtime-dir",
        str(runtime_dir),
        "--task-file",
        str(task_file),
        "--timeout",
        timeout,
        "--poll-interval",
        poll_interval,
    ]
    if keep_agent:
        argv.append("--keep-agent")
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    # create_worker.py exits 0 on a collected report and 124 on await timeout;
    # both carry a JSON payload on the last stdout line. Anything else is an error.
    if proc.returncode not in (0, 124):
        raise AgentRunError(
            f"create_worker run exited {proc.returncode}: {proc.stderr.strip()[:500]}"
        )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    if not lines:
        raise AgentRunError("create_worker run produced no JSON result line")
    try:
        payload = json.loads(lines[-1])
    except ValueError as exc:
        raise AgentRunError(f"create_worker run result was not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise AgentRunError("create_worker run result was not a JSON object")
    return payload


async def run_agent(
    *,
    name: str,
    template: str,
    runtime_dir: Path,
    task_file: Path,
    service_name: str,
    timeout: str = "30m",
    poll_interval: str = "5s",
    keep_agent: bool = False,
    repo_root: Path | None = None,
    runner: Callable[..., Mapping[str, object]] = _run_create_worker_blocking,
) -> AgentResult:
    """Pattern 1: launch a tightly-scoped full agent, wait, collect, destroy.

    Thin wrapper over ``create_worker.py run``. The caller writes the task file
    (with ``lead_agent`` / ``finish_report_path`` frontmatter) under ``runtime_dir``
    first. Returns the structured terminal result; what to do with the worker's
    branch (merge / review) is the caller's concern (a separate future skill).
    """
    root = Path.cwd() if repo_root is None else repo_root
    payload = await to_thread.run_sync(
        lambda: runner(
            name=name,
            template=template,
            runtime_dir=runtime_dir,
            task_file=task_file,
            timeout=timeout,
            poll_interval=poll_interval,
            keep_agent=keep_agent,
            repo_root=root,
        )
    )
    result = _agent_result_from_payload(payload)
    logger.info(
        "ai_integration agent: service={} name={} outcome={} branch={}",
        service_name,
        name,
        result.outcome.value,
        result.branch,
    )
    return result
