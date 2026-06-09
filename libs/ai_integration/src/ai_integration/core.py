"""The three escalating-agency entry points services call.

``run_completion`` -- pattern 3, no agency: direct Anthropic API when a key is
present (always cheaper for non-agentic work), else the ``claude -p`` fallback.
The routing is implicit (by key presence), so a keyless service works immediately
and adding a key later transparently upgrades it; the keyless path also logs the
calculated savings a key would unlock.

``run_task`` -- pattern 2, one-shot agentic: always ``claude -p`` (it can use
tools / read files, which the plain API call cannot).

``run_agent`` -- pattern 1, full agent: a thin wrapper over the synchronous
``create_worker.py launch-sync`` launch -> await -> collect -> destroy path.
"""

import json
import os
import subprocess
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path

from anyio import to_thread
from imbue.imbue_common.frozen_model import FrozenModel
from loguru import logger
from pydantic import ConfigDict, ValidationError

from ai_integration import backends
from ai_integration.credentials import (
    build_claude_cli_env,
    get_api_key,
    require_credentials,
)
from ai_integration.data_types import (
    AgentOutcome,
    AgentResult,
    AnthropicCompletionOptions,
    CompletionResult,
)
from ai_integration.errors import AgentRunError
from ai_integration.pricing import DEFAULT_MODEL, counterfactual_direct_api_cost_usd
from ai_integration.spend import SpendTracker, format_usd, load_spend_tracker

_CREATE_WORKER_REL = ".agents/skills/launch-task/scripts/create_worker.py"

# Type aliases for the injectable backends (tests pass fakes).
_ApiBackend = Callable[..., Awaitable[CompletionResult]]
_CliBackend = Callable[..., Awaitable[CompletionResult]]
# Resolves the per-service spend tracker from services.toml by service_name.
# Injectable so tests can supply a tracker (or None) without a real services.toml.
_SpendLoader = Callable[[str], SpendTracker | None]
# The subprocess runner is injectable so the ``create_worker`` launch boundary
# can be exercised without spawning a real ``uv run`` (mirrors the api/cli
# backend and ``runner`` injection seams used elsewhere in this module).
_SubprocessRun = Callable[..., subprocess.CompletedProcess[str]]


def _log_keyless_savings(result: CompletionResult, prompt: str, model: str) -> None:
    """Log the calculated savings a direct-API key would unlock for this call.

    Only meaningful on the ``claude -p`` fallback (where ``cost_usd`` is the actual
    reported cost). ``result.cost_usd`` already reflects the lean fallback config
    (``--system-prompt`` + ``--tools ""``), so this is an honest stripped-cli vs
    direct-API comparison, not a comparison against the heavier default agent. The
    counterfactual prices just the user's prompt + response -- a direct call carries
    none of ``claude -p``'s system-prompt / tool-definition / CLAUDE.md overhead.
    """
    if result.cost_usd is None:
        return
    counterfactual = counterfactual_direct_api_cost_usd(model, prompt, result.text)
    if counterfactual is None or counterfactual >= result.cost_usd:
        return
    logger.info(
        "ai_integration: this claude -p call cost ~{}; the same call via the "
        "direct Anthropic API would cost ~{} (estimate). Set ANTHROPIC_API_KEY "
        "to save ~{} per call.",
        format_usd(result.cost_usd),
        format_usd(counterfactual),
        format_usd(result.cost_usd - counterfactual),
    )


def _record_spend(
    spend_tracker: SpendTracker | None,
    result: CompletionResult,
    *,
    model: str,
    service_name: str,
) -> None:
    """Record a paid call's cost, or loudly warn when the cost is unknown.

    A call whose ``cost_usd`` is ``None`` (e.g. a direct-API model missing from the
    price table) would otherwise be skipped silently, letting real spend escape the
    ceiling. We can't record an unknown cost, but we must make the gap observable.
    """
    if spend_tracker is None:
        return
    if result.cost_usd is not None:
        spend_tracker.record(result.cost_usd)
        return
    logger.warning(
        "ai_integration: spend NOT recorded for service={} model={} -- the call's "
        "cost could not be determined (model likely missing from the price table), so "
        "it does not count against the ceiling. Add the model to ai_integration.pricing.",
        service_name,
        model,
    )


async def run_completion(
    prompt: str,
    *,
    system: str,
    service_name: str,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 1024,
    env: Mapping[str, str] | None = None,
    anthropic_options: AnthropicCompletionOptions | None = None,
    strip_mngr_agent_vars: bool = False,
    claude_cli_args: Sequence[str] | None = None,
    api_backend: _ApiBackend = backends.complete_via_api,
    cli_backend: _CliBackend = backends.complete_via_cli,
    spend_loader: _SpendLoader = load_spend_tracker,
) -> CompletionResult:
    """Pattern 3: one non-agentic completion, direct API if keyed else ``claude -p``.

    ``system`` is **required**. On the direct-API path it is the (cache-controlled)
    system block. On the keyless ``claude -p`` fallback it is passed as
    ``--system-prompt`` *and* tools are disabled (``--tools ""``), which keeps the
    call lean and -- critically -- prevents the auto-discovered CLAUDE.md from
    hijacking the response. ``claude -p`` is non-bare on the keyless path (bare
    can't authenticate without an API key), so CLAUDE.md is always loaded; with an
    empty/absent system prompt the model answers *that* ambient text instead of the
    user's prompt. Requiring ``system`` makes the neutralizing prompt mandatory by
    construction, so both backends share the same ``system`` and behave consistently
    even though the CLI fallback can't be made fully context-free.

    Spend tracking is automatic and **opt-in via ``services.toml``**: the spend
    tracker is resolved from ``[services.<service_name>.ai_spend]`` (no tracker
    object to thread through calls). If the service configured a ceiling, this
    checks it before the call and records the cost after; if not, the call runs
    unbounded. Spend aggregates per service across every call via the persisted
    ledger.
    """
    resolved_env = os.environ if env is None else env
    spend_tracker = spend_loader(service_name)
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
        if anthropic_options:
            logger.warning(
                "ai_integration: anthropic_options were passed but are IGNORED on the "
                "keyless claude -p fallback (service={}); they only apply on the direct "
                "Anthropic API path. Structured output / tools need ANTHROPIC_API_KEY, "
                "or pass equivalent flags via claude_cli_args.",
                service_name,
            )
        require_credentials(resolved_env)
        cli_env = build_claude_cli_env(resolved_env, strip_mngr_agent_vars)
        # Run from an isolated working directory so claude -p does not auto-load this
        # repo's CLAUDE.md / .claude hooks (which otherwise leak into -- and can hijack
        # -- a non-agentic completion's answer). Credentials come from the env, not the
        # cwd, so auth is unaffected.
        with tempfile.TemporaryDirectory(prefix="ai_integration_completion_") as cwd:
            result = await cli_backend(
                model=model,
                prompt=prompt,
                env=cli_env,
                system=system,
                tools="",
                cwd=cwd,
                extra_args=claude_cli_args,
            )
        _log_keyless_savings(result, prompt, model)

    logger.info(
        "ai_integration completion: service={} model={} billing={} cost_usd={}",
        service_name,
        model,
        result.billing_path.value,
        result.cost_usd,
    )
    _record_spend(spend_tracker, result, model=model, service_name=service_name)
    return result


async def run_task(
    prompt: str,
    *,
    service_name: str,
    model: str = DEFAULT_MODEL,
    system: str | None = None,
    append_system: str | None = None,
    permission_mode: str | None = "bypassPermissions",
    env: Mapping[str, str] | None = None,
    strip_mngr_agent_vars: bool = False,
    claude_cli_args: Sequence[str] | None = None,
    cli_backend: _CliBackend = backends.complete_via_cli,
    spend_loader: _SpendLoader = load_spend_tracker,
) -> CompletionResult:
    """Pattern 2: one-shot agentic task via headless ``claude -p`` (tools/file access).

    Unlike ``run_completion``, ``system`` / ``append_system`` are *optional* and
    tools stay enabled: the point of an agentic task is to ride on Claude Code's
    default agent (its tools, file access, and base system prompt). ``append_system``
    (``--append-system-prompt``) layers task instructions on top of that default;
    pass ``system`` (``--system-prompt``) only to fully replace it. ``--bare`` is
    not used -- it would strip the agent and, keyless, can't authenticate.

    ``permission_mode`` maps to ``--permission-mode`` and defaults to
    ``"bypassPermissions"``. This default is **load-bearing**: headless ``claude -p``
    has no human to approve tool use, so under the normal mode the agent's Read /
    Write / Bash calls are auto-denied and the "agentic" task can't actually touch
    files (it just replies that it needs permission). Tighten it (e.g. ``acceptEdits``)
    or set it to ``None`` to omit the flag and drive permissions yourself via
    ``claude_cli_args`` (e.g. an ``--allowedTools`` list). Safety here comes from the
    tight task scope and the spend ceiling, not from per-tool prompts that can't be
    answered headlessly.

    Spend tracking is automatic and opt-in via ``services.toml`` (resolved from
    ``[services.<service_name>.ai_spend]``), the same as ``run_completion``.
    """
    resolved_env = os.environ if env is None else env
    spend_tracker = spend_loader(service_name)
    if spend_tracker is not None:
        spend_tracker.check_ceiling()
    require_credentials(resolved_env)
    cli_env = build_claude_cli_env(resolved_env, strip_mngr_agent_vars)
    result = await cli_backend(
        model=model,
        prompt=prompt,
        env=cli_env,
        system=system,
        append_system=append_system,
        permission_mode=permission_mode,
        extra_args=claude_cli_args,
    )
    logger.info(
        "ai_integration task: service={} model={} billing={} cost_usd={}",
        service_name,
        model,
        result.billing_path.value,
        result.cost_usd,
    )
    _record_spend(spend_tracker, result, model=model, service_name=service_name)
    return result


class _CreateWorkerResult(FrozenModel):
    """The JSON ``create_worker.py launch-sync`` writes to its ``--result-json``.

    Validated at the boundary so a shape change in ``create_worker`` (a real bug)
    fails loudly here instead of silently producing a blank/wrong ``AgentResult``.
    ``timed_out`` / ``body`` / ``branch`` are always emitted by the launcher and so
    are required; ``type`` / ``name`` come from the worker's report frontmatter and
    may be null; ``raw_report`` is omitted on the timeout path and defaults to "".
    ``extra="ignore"`` keeps us tolerant of new launcher fields.
    """

    model_config = ConfigDict(extra="ignore")

    timed_out: bool
    body: str
    branch: str
    type: str | None = None
    name: str | None = None
    raw_report: str = ""


def _outcome_for(result: _CreateWorkerResult) -> AgentOutcome:
    # Maps the worker report's open-ended ``name`` string onto the normalized
    # outcome enum. ``name`` is a free-form report field (not itself an enum), so
    # the catch-all ``UNKNOWN`` is the deliberate fallback rather than an
    # ``assert_never`` exhaustiveness check -- an unrecognized name must NOT raise.
    if result.timed_out:
        return AgentOutcome.TIMED_OUT
    match result.name:
        case "done":
            return AgentOutcome.DONE
        case "stuck":
            return AgentOutcome.STUCK
        case "no-update-needed":
            return AgentOutcome.NO_UPDATE_NEEDED
        case _:
            return AgentOutcome.UNKNOWN


def _agent_result_from_payload(payload: Mapping[str, object]) -> AgentResult:
    try:
        result = _CreateWorkerResult.model_validate(payload)
    except ValidationError as exc:
        raise AgentRunError(
            f"create_worker launch-sync result did not match the expected shape: {exc}"
        ) from exc
    return AgentResult(
        outcome=_outcome_for(result),
        report_type=result.type,
        report_name=result.name,
        body=result.body,
        branch=result.branch,
        raw_report=result.raw_report,
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
    subprocess_run: _SubprocessRun = subprocess.run,
) -> Mapping[str, object]:
    # Collect the result from a dedicated ``--result-json`` file rather than
    # scraping stdout: create_worker's ``launch-sync`` interleaves human-readable
    # launch messages (and the worker's mngr-destroy output) on stdout, so picking
    # "the last line" is fragile. A file the caller names is the unambiguous contract.
    with tempfile.TemporaryDirectory(prefix="ai_integration_run_") as tmp:
        result_path = Path(tmp) / "result.json"
        argv = [
            "uv",
            "run",
            str(repo_root / _CREATE_WORKER_REL),
            "launch-sync",
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
            "--result-json",
            str(result_path),
        ]
        if keep_agent:
            argv.append("--keep-agent")
        # ``cwd=repo_root`` so ``uv run`` resolves the uv project from the same
        # root the ``create_worker.py`` script path is anchored to. ``uv run``
        # picks its project by the working directory, not the script path, so a
        # caller running from elsewhere (the reason ``repo_root`` is a parameter)
        # would otherwise get the wrong project or none at all.
        proc = subprocess_run(
            argv, capture_output=True, text=True, check=False, cwd=str(repo_root)
        )
        # create_worker.py exits 0 on a collected report and 124 on await
        # timeout; both write the JSON payload to ``--result-json``. Any other
        # exit code means it failed before producing a result.
        if proc.returncode not in (0, 124):
            raise AgentRunError(
                f"create_worker launch-sync exited {proc.returncode}: "
                f"{proc.stderr.strip()[:500]}"
            )
        if not result_path.is_file():
            raise AgentRunError(
                "create_worker launch-sync produced no result-json file "
                f"(exit {proc.returncode}): {proc.stderr.strip()[:500]}"
            )
        raw = result_path.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise AgentRunError(
            f"create_worker launch-sync result was not JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise AgentRunError("create_worker launch-sync result was not a JSON object")
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

    Thin wrapper over ``create_worker.py launch-sync``. The caller writes the task file
    (with ``lead_agent`` / ``finish_report_path`` frontmatter) under ``runtime_dir``
    first. Returns the structured terminal result; what to do with the worker's
    branch (merge / review) is the caller's concern.

    ``repo_root`` locates ``create_worker.py``; it defaults to the current working
    directory, which matches this repo's "cwd = repo root" convention (services
    run from the checkout root). Pass it explicitly when the caller runs from
    elsewhere.
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
