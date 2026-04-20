"""Hermes plugin that mirrors the template's Claude Code guardrail hooks.

The plugin delegates to the same shell scripts under ``scripts/`` that the
Claude Code ``.claude/settings.json`` hooks invoke, so the behaviour is
defined in one place and both agent runtimes share it.

Note that hermes plugin hooks are fire-and-forget observers (their return
value is ignored), so unlike Claude Code's ``exit 2`` mechanism these hooks
cannot actually block a tool call or refuse a session end -- they only
surface a warning to the user. The shared shell scripts still communicate
violations via exit code 2; the plugin prints a warning when that happens.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_TERMINAL_TOOLS = {"terminal", "process"}


def _scripts_dir() -> Path:
    """Locate the template's scripts directory from the agent work directory."""
    workdir = os.environ.get("MNGR_AGENT_WORK_DIR")
    base = Path(workdir) if workdir else Path.cwd()
    return base / "scripts"


def _run_guard(script: Path, stdin: str, label: str) -> None:
    if not script.exists():
        logger.debug("Guard script not found, skipping: %s", script)
        return
    try:
        result = subprocess.run(
            ["bash", str(script)],
            input=stdin,
            text=True,
            capture_output=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("%s guard failed to run: %s", label, exc)
        return
    if result.returncode == 2:
        message = result.stderr.strip() or f"{label} guard tripped"
        # Fire-and-forget: hermes plugin hooks cannot block, so surface the
        # message to the user via stderr so it shows up in the CLI output.
        print(f"[forever_template_guards] {message}", flush=True)


def _extract_shell_command(tool_name: str, args: dict[str, Any]) -> str | None:
    """Return the shell command about to run, if this tool invocation runs one.

    Hermes' built-in ``terminal`` tool takes the command under the ``command``
    key. Other tools (file ops, web search, etc.) don't execute shell
    commands and are ignored by this guard.
    """
    if tool_name not in _TERMINAL_TOOLS:
        return None
    command = args.get("command")
    if not isinstance(command, str):
        return None
    return command


def pre_tool_call(tool_name: str, args: dict[str, Any], task_id: str, **kwargs: Any) -> None:
    """Warn when a terminal tool is about to run a prohibited git command."""
    command = _extract_shell_command(tool_name, args)
    if command is None:
        return
    payload = json.dumps({"tool_name": tool_name, "tool_input": {"command": command}})
    _run_guard(_scripts_dir() / "guard_commit_rewrite.sh", payload, "commit-rewrite")


def on_session_end(session_id: str, completed: bool, interrupted: bool, **kwargs: Any) -> None:
    """Remind the agent to end in the repo root, matching Claude's Stop hook."""
    _run_guard(_scripts_dir() / "check_repo_root.sh", "", "repo-root")


def register(ctx: Any) -> None:
    """Entry point invoked by hermes when the plugin is loaded."""
    ctx.register_hook("pre_tool_call", pre_tool_call)
    ctx.register_hook("on_session_end", on_session_end)
