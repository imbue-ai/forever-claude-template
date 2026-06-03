"""The two completion backends: the direct Anthropic API and headless ``claude -p``.

Both are ``async``. The direct-API path uses ``AsyncAnthropic`` and enables prompt
caching on the system prompt. The ``claude -p`` path runs the CLI as a blocking
subprocess offloaded to a worker thread via ``anyio`` (no raw asyncio), reading
its ``--output-format json`` usage/cost so callers can price and compare.
"""

import json
import subprocess
from collections.abc import Mapping, Sequence

from anthropic import AsyncAnthropic
from anyio import to_thread

from ai_integration.data_types import BillingPath, CompletionResult, Usage
from ai_integration.errors import ClaudeCLIError
from ai_integration.pricing import estimate_cost_usd


async def complete_via_api(
    *,
    api_key: str,
    model: str,
    prompt: str,
    system: str | None = None,
    max_tokens: int = 1024,
    options: Mapping[str, object] | None = None,
) -> CompletionResult:
    """One non-agentic completion through the direct Anthropic API.

    ``options`` is passed straight through to ``messages.create`` so any Anthropic
    API parameter (tools, response formats, temperature, etc.) is usable. The
    system prompt is sent as a cache-controlled block to enable prompt caching.
    """
    client = AsyncAnthropic(api_key=api_key)
    kwargs: dict[str, object] = dict(options or {})
    kwargs.setdefault("model", model)
    kwargs.setdefault("max_tokens", max_tokens)
    kwargs.setdefault("messages", [{"role": "user", "content": prompt}])
    if system is not None and "system" not in kwargs:
        kwargs["system"] = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ]
    response = await client.messages.create(**kwargs)  # type: ignore[arg-type]
    text = "".join(
        getattr(block, "text", "")
        for block in response.content
        if getattr(block, "type", None) == "text"
    )
    usage = Usage(
        input_tokens=getattr(response.usage, "input_tokens", 0) or 0,
        output_tokens=getattr(response.usage, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(response.usage, "cache_creation_input_tokens", 0)
        or 0,
    )
    return CompletionResult(
        text=text,
        billing_path=BillingPath.DIRECT_API,
        model=model,
        usage=usage,
        cost_usd=estimate_cost_usd(model, usage),
    )


def parse_cli_result(data: object, model: str) -> CompletionResult:
    """Build a ``CompletionResult`` from ``claude -p --output-format json`` output."""
    if not isinstance(data, dict):
        raise ClaudeCLIError("claude -p JSON output was not an object")
    raw_usage = data.get("usage")
    usage_dict = raw_usage if isinstance(raw_usage, dict) else {}
    usage = Usage(
        input_tokens=int(usage_dict.get("input_tokens", 0) or 0),
        output_tokens=int(usage_dict.get("output_tokens", 0) or 0),
        cache_read_tokens=int(usage_dict.get("cache_read_input_tokens", 0) or 0),
        cache_write_tokens=int(usage_dict.get("cache_creation_input_tokens", 0) or 0),
    )
    cost = data.get("total_cost_usd")
    text = data.get("result")
    return CompletionResult(
        text=text if isinstance(text, str) else "",
        billing_path=BillingPath.CLAUDE_CLI,
        model=model,
        usage=usage,
        cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
    )


def _run_claude_cli_blocking(
    *,
    prompt: str,
    model: str,
    env: Mapping[str, str],
    extra_args: Sequence[str] | None,
) -> object:
    argv = ["claude", "-p", prompt, "--output-format", "json"]
    if model:
        argv += ["--model", model]
    argv += list(extra_args or [])
    proc = subprocess.run(
        argv, capture_output=True, text=True, env=dict(env), check=False
    )
    if proc.returncode != 0:
        raise ClaudeCLIError(
            f"claude -p exited {proc.returncode}: {proc.stderr.strip()[:500]}"
        )
    try:
        return json.loads(proc.stdout)
    except ValueError as exc:
        raise ClaudeCLIError(f"claude -p output was not valid JSON: {exc}") from exc


async def complete_via_cli(
    *,
    model: str,
    prompt: str,
    env: Mapping[str, str],
    extra_args: Sequence[str] | None = None,
) -> CompletionResult:
    """One completion/agentic run through headless ``claude -p``.

    Runs the CLI in a worker thread (so the async caller isn't blocked) and parses
    its JSON usage/cost. ``env`` should be built via
    ``credentials.build_claude_cli_env`` so ``MAIN_CLAUDE_SESSION_ID`` is unset.
    """
    data = await to_thread.run_sync(
        lambda: _run_claude_cli_blocking(
            prompt=prompt, model=model, env=env, extra_args=extra_args
        )
    )
    return parse_cli_result(data, model)
