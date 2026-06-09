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
from anthropic.types import (
    ContentBlock,
    Message,
    TextBlock,
    ToolUseBlock,
    message_create_params,
)
from anyio import to_thread
from imbue.imbue_common.frozen_model import FrozenModel
from pydantic import ConfigDict, ValidationError

from ai_integration.data_types import (
    AnthropicCompletionOptions,
    BillingPath,
    CompletionResult,
    ToolCall,
    Usage,
)
from ai_integration.errors import ClaudeCLIError
from ai_integration.pricing import estimate_cost_usd


async def complete_via_api(
    *,
    api_key: str,
    model: str,
    prompt: str,
    system: str | None = None,
    max_tokens: int = 1024,
    options: AnthropicCompletionOptions | None = None,
) -> CompletionResult:
    """One non-agentic completion through the direct Anthropic API.

    ``options`` (an ``AnthropicCompletionOptions``) is merged straight into
    ``messages.create`` so any overridable Anthropic API parameter (tools, tool_choice,
    temperature, etc.) is usable, with full type-checking on the values -- it mirrors
    the SDK's optional message params, reusing the SDK's own value types. The system
    prompt is sent as a cache-controlled block to enable prompt caching. Caller-supplied
    ``options`` win over the defaults below.
    """
    params: message_create_params.MessageCreateParamsNonStreaming = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system is not None:
        params["system"] = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ]
    if options:
        params = {**params, **options}
    # ``async with`` so the client's httpx connection pool is always released --
    # a new client is built per call, and leaking the pool would accumulate open
    # connections across a high-volume completion flow.
    async with AsyncAnthropic(api_key=api_key) as client:
        response = await client.messages.create(**params)
    return build_api_result(response, model)


def build_api_result(response: Message, requested_model: str) -> CompletionResult:
    """Assemble a ``CompletionResult`` from an Anthropic ``messages.create`` response.

    Pure (reads only ``response.content`` / ``.usage`` / ``.model``) so it is
    unit-testable by constructing a ``Message`` directly, without a live
    ``AsyncAnthropic`` client. The reported ``model`` is the *served* model
    (``response.model``) -- which honors ``CompletionResult.model``'s "served by"
    contract, since an alias can resolve to a dated snapshot -- falling back to
    ``requested_model`` only if the response omits it. Cost is estimated from the
    served model's price so the figure matches what was actually billed.
    """
    text, tool_calls = parse_api_content(response.content)
    usage = Usage(
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        cache_read_tokens=response.usage.cache_read_input_tokens or 0,
        cache_write_tokens=response.usage.cache_creation_input_tokens or 0,
    )
    model = response.model or requested_model
    return CompletionResult(
        text=text,
        billing_path=BillingPath.DIRECT_API,
        model=model,
        tool_calls=tool_calls,
        usage=usage,
        cost_usd=estimate_cost_usd(model, usage),
    )


class _ClaudeCliUsage(FrozenModel):
    """The ``usage`` sub-object of a ``claude -p`` JSON result.

    ``extra="ignore"`` so new SDK usage fields never break parsing. Token counts
    are required (the documented ``NonNullableUsage`` shape always reports them);
    the cache fields default to 0 since they are only present when caching ran.
    """

    model_config = ConfigDict(extra="ignore")

    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


class _ClaudeCliResult(FrozenModel):
    """The ``--output-format json`` result message from ``claude -p``.

    Validated at the boundary so malformed output fails loudly instead of silently
    degrading. The required envelope (``subtype`` / ``is_error`` / ``usage`` /
    ``total_cost_usd``) is present on both the success and the error arms of the
    documented ``SDKResultMessage`` union; ``result`` is present only on the success
    arm, and ``errors`` only on the error arm, so both are optional here and the
    arm is disambiguated by ``subtype`` / ``is_error`` in ``parse_cli_result``.
    ``extra="ignore"`` keeps us forward-compatible with new CLI fields.
    """

    model_config = ConfigDict(extra="ignore")

    subtype: str
    is_error: bool
    usage: _ClaudeCliUsage
    total_cost_usd: float
    result: str | None = None
    errors: tuple[str, ...] = ()


def parse_api_content(
    content: Sequence[ContentBlock],
) -> tuple[str, tuple[ToolCall, ...]]:
    """Split an Anthropic ``messages.create`` response into text and tool calls.

    Concatenates ``TextBlock`` content into the plain completion text and collects
    ``ToolUseBlock``s (the structured-output channel) into ``ToolCall``s, narrowing
    the SDK's ``ContentBlock`` union with ``isinstance``. A forced tool call yields
    empty text and a populated tuple, so the structured-output data is surfaced
    rather than lost. Other block kinds (thinking, server-tool results, etc.) are
    ignored. Pure, so it is unit-testable by constructing blocks directly.
    """
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in content:
        if isinstance(block, TextBlock):
            text_parts.append(block.text)
        elif isinstance(block, ToolUseBlock):
            tool_calls.append(ToolCall(id=block.id, name=block.name, input=block.input))
    return "".join(text_parts), tuple(tool_calls)


def parse_cli_result(data: object, model: str) -> CompletionResult:
    """Build a ``CompletionResult`` from ``claude -p --output-format json`` output.

    The payload is validated against ``_ClaudeCliResult``; output that does not match
    the documented shape (missing ``usage`` / ``total_cost_usd``, wrong types, not a
    JSON object) raises ``ClaudeCLIError`` rather than silently degrading. An *error*
    result (``is_error`` true, e.g. ``error_max_turns`` / ``error_during_execution``)
    also raises -- surfacing the failure with the worker's ``errors`` instead of
    returning an empty-text "success", which is how a maxed-out or failed run would
    otherwise slip through unnoticed.
    """
    try:
        parsed = _ClaudeCliResult.model_validate(data)
    except ValidationError as exc:
        raise ClaudeCLIError(
            f"claude -p JSON output did not match the expected result shape: {exc}"
        ) from exc
    if parsed.is_error or parsed.subtype != "success":
        detail = "; ".join(parsed.errors) or "no error detail reported"
        raise ClaudeCLIError(
            f"claude -p returned an error result (subtype={parsed.subtype}): {detail}"
        )
    if parsed.result is None:
        raise ClaudeCLIError(
            "claude -p success result was missing the 'result' text field"
        )
    usage = Usage(
        input_tokens=parsed.usage.input_tokens,
        output_tokens=parsed.usage.output_tokens,
        cache_read_tokens=parsed.usage.cache_read_input_tokens,
        cache_write_tokens=parsed.usage.cache_creation_input_tokens,
    )
    return CompletionResult(
        text=parsed.result,
        billing_path=BillingPath.CLAUDE_CLI,
        model=model,
        usage=usage,
        cost_usd=parsed.total_cost_usd,
    )


def build_claude_cli_argv(
    *,
    prompt: str,
    model: str,
    system: str | None,
    append_system: str | None,
    tools: str | None,
    permission_mode: str | None,
    extra_args: Sequence[str] | None,
) -> list[str]:
    """Build the ``claude -p`` argv. Pure, so flag emission is unit-testable.

    ``--system-prompt`` *replaces* the default Claude Code system prompt;
    ``--append-system-prompt`` adds to it. ``--tools ""`` disables all tools.
    ``tools`` is checked against ``None`` (not falsiness) because the empty string
    is the meaningful "disable every tool" value, distinct from "leave the flag off
    and inherit the default tool set".

    ``permission_mode`` maps to ``--permission-mode``. Headless ``claude -p`` cannot
    prompt a human, so a tool that would need approval is otherwise auto-denied --
    which is why an agentic ``run_task`` defaults this to ``bypassPermissions`` (no
    flag is emitted when it is ``None``).
    """
    argv = ["claude", "-p", prompt, "--output-format", "json"]
    if model:
        argv += ["--model", model]
    if system is not None:
        argv += ["--system-prompt", system]
    if append_system is not None:
        argv += ["--append-system-prompt", append_system]
    if tools is not None:
        argv += ["--tools", tools]
    if permission_mode is not None:
        argv += ["--permission-mode", permission_mode]
    argv += list(extra_args or [])
    return argv


def _run_claude_cli_blocking(
    *,
    prompt: str,
    model: str,
    env: Mapping[str, str],
    system: str | None,
    append_system: str | None,
    tools: str | None,
    permission_mode: str | None,
    cwd: str | None,
    extra_args: Sequence[str] | None,
) -> object:
    argv = build_claude_cli_argv(
        prompt=prompt,
        model=model,
        system=system,
        append_system=append_system,
        tools=tools,
        permission_mode=permission_mode,
        extra_args=extra_args,
    )
    proc = subprocess.run(
        argv, capture_output=True, text=True, env=dict(env), check=False, cwd=cwd
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
    system: str | None = None,
    append_system: str | None = None,
    tools: str | None = None,
    permission_mode: str | None = None,
    cwd: str | None = None,
    extra_args: Sequence[str] | None = None,
) -> CompletionResult:
    """One completion/agentic run through headless ``claude -p``.

    Runs the CLI in a worker thread (so the async caller isn't blocked) and parses
    its JSON usage/cost. ``env`` should be built via
    ``credentials.build_claude_cli_env`` so ``MAIN_CLAUDE_SESSION_ID`` is unset.

    ``cwd`` sets the subprocess working directory. ``claude -p`` auto-discovers the
    project ``CLAUDE.md`` and ``.claude`` hooks from the working directory, so the
    non-agentic completion path passes an isolated temp dir to keep that ambient
    project context (and its hook-injected reminders) out of the prompt. ``None``
    inherits the caller's cwd, which is what the agentic ``run_task`` path wants.

    ``system`` maps to ``--system-prompt`` (replacing Claude Code's default agent
    system prompt) and ``append_system`` to ``--append-system-prompt``. ``tools``
    maps to ``--tools`` -- pass ``""`` to disable all tools (the non-agentic
    completion path does this so the call answers the prompt and nothing else).
    These flags are how a non-agentic ``claude -p`` call sheds most of the default
    agent's per-call context overhead; note they do *not* drop the auto-discovered
    CLAUDE.md / skills, which only ``--bare`` removes -- and ``--bare`` requires an
    API key, so it is unavailable on the keyless subscription path.
    """
    data = await to_thread.run_sync(
        lambda: _run_claude_cli_blocking(
            prompt=prompt,
            model=model,
            env=env,
            system=system,
            append_system=append_system,
            tools=tools,
            permission_mode=permission_mode,
            cwd=cwd,
            extra_args=extra_args,
        )
    )
    return parse_cli_result(data, model)
