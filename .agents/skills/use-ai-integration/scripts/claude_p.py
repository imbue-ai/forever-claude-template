#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["anyio"]
# ///
"""Copyable helper for calling headless ``claude -p`` from a service.

COPY THIS FILE into your service and adapt it -- it is a reference snippet, not an
importable package. It is the **keyless** path for an AI-driven service: when
``ANTHROPIC_API_KEY`` is set, call ``litellm`` directly instead (cheaper for
non-agentic work; see the use-ai-integration skill). When no key is set,
``claude -p`` runs on the local Claude subscription's programmatic pool.

Two entry points cover the two non-agent scenarios; both share one core that
handles the things that are easy to get wrong by hand:

- ``claude_p_completion(prompt, *, system, model=...)`` -- a non-agentic
  completion (classify / summarize / extract / rewrite / answer-from-context).
  Disables all tools (``--tools ""``) **and** runs from an isolated temp directory
  so ``claude -p`` does not auto-discover the repo's ``CLAUDE.md`` / ``.claude``
  hooks (which otherwise bleed into -- and intermittently hijack -- the answer).
  ``system`` is required: it frames the task and is the neutralizing instruction.
  (``--bare`` would also strip that project context, but it cannot authenticate
  without an API key, so the isolated cwd is the keyless workaround.)

- ``claude_p_task(prompt, *, append_system=None, system=None, model=...,
  permission_mode="bypassPermissions")`` -- a one-shot agentic task that needs
  tools / file access. Tools stay enabled and it runs in the current working
  directory (the repo). ``bypassPermissions`` is load-bearing: a headless run has
  no human to approve tool use, so otherwise Read/Write/Bash are auto-denied.

Both unset ``MAIN_CLAUDE_SESSION_ID`` in the child environment (an inherited value
makes the child look like mngr's managed main session and trips its
stop/readiness hooks), request ``--output-format json``, run the blocking
subprocess off the event loop (so an async service is not blocked), and raise on a
non-zero exit or a ``claude -p`` error result rather than silently returning empty
text.
"""

from __future__ import annotations

import functools
import json
import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from anyio import to_thread

_MAIN_CLAUDE_SESSION_ID = "MAIN_CLAUDE_SESSION_ID"
# mngr identity vars its own subagent proxy strips; dropping them is defense in
# depth (the session-hook fix only needs MAIN_CLAUDE_SESSION_ID unset).
_MNGR_AGENT_VARS = ("MNGR_AGENT_STATE_DIR", "MNGR_AGENT_NAME", "MNGR_HOST_DIR")

_DEFAULT_MODEL = "claude-haiku-4-5"


class ClaudeCLIError(RuntimeError):
    """A ``claude -p`` invocation failed or returned unparseable / error output."""


@dataclass(frozen=True)
class Usage:
    """Token counts from a ``claude -p`` run, for cost / savings estimation."""

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class ClaudeResult:
    """The parsed result of a ``claude -p --output-format json`` run."""

    text: str
    cost_usd: float
    usage: Usage
    raw: dict[str, object]  # the verbatim JSON object claude -p emitted


def _child_env(strip_mngr_agent_vars: bool = False) -> dict[str, str]:
    """Build the child environment: a copy of os.environ minus the session var."""
    env = dict(os.environ)
    env.pop(_MAIN_CLAUDE_SESSION_ID, None)
    if strip_mngr_agent_vars:
        for var in _MNGR_AGENT_VARS:
            env.pop(var, None)
    return env


def _build_argv(
    prompt: str,
    *,
    model: str,
    system: str | None,
    append_system: str | None,
    tools: str | None,
    permission_mode: str | None,
) -> list[str]:
    """Assemble the ``claude -p`` argv. Pure, so flag emission is unit-testable.

    ``tools`` is checked against ``None`` (not falsiness): the empty string is the
    meaningful "disable every tool" value, distinct from "leave the flag off and
    inherit the default tool set".
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
    return argv


def _parse_result(data: object) -> ClaudeResult:
    """Build a ``ClaudeResult`` from parsed ``claude -p`` JSON, or raise.

    The result message has a **success arm** (``subtype == "success"`` with a
    ``result`` string) and an **error arm** (``is_error`` true, e.g.
    ``error_max_turns`` / ``error_during_execution``, carrying ``errors``). The
    error arm and a missing ``result`` both raise, so a maxed-out or failed run
    surfaces instead of looking like an empty-text success.
    """
    if not isinstance(data, dict):
        raise ClaudeCLIError("claude -p JSON output was not an object")
    if data.get("is_error") or data.get("subtype") != "success":
        errors = data.get("errors")
        # claude -p output is external JSON, so coerce each element to str: a
        # non-string entry would otherwise make str.join raise TypeError inside
        # this error path, masking the ClaudeCLIError we are trying to raise.
        detail = "; ".join(str(e) for e in errors) if isinstance(errors, list) else ""
        raise ClaudeCLIError(
            f"claude -p returned an error result (subtype={data.get('subtype')!r}): "
            f"{detail or 'no error detail reported'}"
        )
    result_text = data.get("result")
    if not isinstance(result_text, str):
        raise ClaudeCLIError("claude -p success result was missing the 'result' text")
    raw_usage = data.get("usage")
    raw_usage = raw_usage if isinstance(raw_usage, dict) else {}
    usage = Usage(
        input_tokens=int(raw_usage.get("input_tokens", 0) or 0),
        output_tokens=int(raw_usage.get("output_tokens", 0) or 0),
        cache_read_tokens=int(raw_usage.get("cache_read_input_tokens", 0) or 0),
        cache_write_tokens=int(raw_usage.get("cache_creation_input_tokens", 0) or 0),
    )
    cost = data.get("total_cost_usd")
    if not isinstance(cost, (int, float)) or isinstance(cost, bool):
        raise ClaudeCLIError("claude -p result was missing a numeric 'total_cost_usd'")
    return ClaudeResult(text=result_text, cost_usd=float(cost), usage=usage, raw=data)


def _run_blocking(
    argv: Sequence[str], *, env: Mapping[str, str], cwd: str | None
) -> ClaudeResult:
    """Run ``claude -p`` synchronously and parse its JSON. Raises on failure."""
    proc = subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        env=dict(env),
        check=False,
        cwd=cwd,
    )
    if proc.returncode != 0:
        raise ClaudeCLIError(
            f"claude -p exited {proc.returncode}: {proc.stderr.strip()[:500]}"
        )
    try:
        data = json.loads(proc.stdout)
    except ValueError as exc:
        raise ClaudeCLIError(f"claude -p output was not valid JSON: {exc}") from exc
    return _parse_result(data)


async def claude_p_completion(
    prompt: str,
    *,
    system: str,
    model: str = _DEFAULT_MODEL,
    strip_mngr_agent_vars: bool = False,
) -> ClaudeResult:
    """One non-agentic completion. ``system`` is required (see module docstring)."""
    env = _child_env(strip_mngr_agent_vars)
    argv = _build_argv(
        prompt,
        model=model,
        system=system,
        append_system=None,
        tools="",
        permission_mode=None,
    )
    # Isolated cwd: claude -p auto-discovers CLAUDE.md / .claude hooks from the
    # working directory, so a throwaway dir keeps that project context out of the
    # answer. Credentials come from the env, not the cwd, so auth is unaffected.
    with tempfile.TemporaryDirectory(prefix="claude_p_completion_") as cwd:
        return await to_thread.run_sync(
            functools.partial(_run_blocking, argv, env=env, cwd=cwd)
        )


async def claude_p_task(
    prompt: str,
    *,
    system: str | None = None,
    append_system: str | None = None,
    model: str = _DEFAULT_MODEL,
    permission_mode: str | None = "bypassPermissions",
    strip_mngr_agent_vars: bool = False,
) -> ClaudeResult:
    """One agentic task: tools enabled, run in the current (repo) working dir.

    ``append_system`` layers task instructions on Claude Code's default agent
    prompt; pass ``system`` to replace it outright (rare -- you usually want the
    default agent here).
    """
    env = _child_env(strip_mngr_agent_vars)
    argv = _build_argv(
        prompt,
        model=model,
        system=system,
        append_system=append_system,
        tools=None,
        permission_mode=permission_mode,
    )
    return await to_thread.run_sync(
        functools.partial(_run_blocking, argv, env=env, cwd=None)
    )
