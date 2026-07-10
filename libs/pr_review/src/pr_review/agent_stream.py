"""Run a headless ``claude -p`` agent and stream its activity to a log file.

Shared by the rich-types "prepare" agent (``prepare.py``) and the per-line
"ask an agent" investigator (``ask.py``). Both spawn a one-shot ``claude -p``
to work against a cached repo checkout and want to show the user live progress
while it runs, so this module owns the common plumbing: the subprocess spawn
(with the env tweaks that let a headless child run and stop cleanly under the
mngr hooks), the stream-json parsing loop, and rendering each event into
human-readable log lines. The caller chooses the working directory: ``prepare``
runs *inside* the checkout (it installs the repo's deps), while ``ask`` runs in
a neutral throwaway dir and reads the checkout by absolute path (so the
read-only investigator never inherits the reviewed repo's hooks / CLAUDE.md).

Each rendered line carries a lightweight kind marker the frontend styles by:
``"● "`` agent narration, ``"$ "`` a shell command, ``"» "`` another tool call,
and no marker for tool output.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path
from typing import NamedTuple


class AgentError(RuntimeError):
    """The headless agent failed to run to completion."""


class AgentRun(NamedTuple):
    text: str  # the agent's final result text
    cost_usd: float


def first_line(text: str, width: int) -> str:
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line if len(line) <= width else line[:width] + " …"


def tail_lines(text: str, count: int, width: int) -> list[str]:
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    tail = lines[-count:]
    return [ln if len(ln) <= width else ln[:width] + " …" for ln in tail]


def render_stream_event(ev: dict) -> list[str]:
    """Turn one ``claude -p`` stream-json event into human-readable log lines.

    Surfaces what the user cares about while waiting -- the shell commands the
    agent runs and their output, plus the agent's own narration -- and drops the
    noise (hooks, thinking, token counters).
    """
    out: list[str] = []
    etype = ev.get("type")
    if etype == "assistant":
        for block in (ev.get("message") or {}).get("content") or []:
            btype = block.get("type")
            if btype == "text":
                for line in (block.get("text") or "").splitlines():
                    if line.strip():
                        out.append("● " + line.strip())
            elif btype == "tool_use":
                name = block.get("name") or "tool"
                inp = block.get("input") or {}
                if name == "Bash":
                    out.append("$ " + first_line(inp.get("command") or "", 300))
                else:
                    target = inp.get("file_path") or inp.get("path") or inp.get("description") or ""
                    out.append(f"» {name} {first_line(str(target), 200)}".rstrip())
    elif etype == "user":
        text = ""
        result = ev.get("tool_use_result")
        if isinstance(result, dict):
            text = result.get("stdout") or ""
            if result.get("stderr"):
                text += ("\n" if text else "") + result["stderr"]
        if not text:
            for block in (ev.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_result":
                    content = block.get("content")
                    if isinstance(content, str):
                        text += content
                    elif isinstance(content, list):
                        text += "".join(p.get("text", "") for p in content if isinstance(p, dict))
        out.extend(tail_lines(text, 12, 300))
    return out


def _agent_env() -> dict:
    env = dict(os.environ)
    # The child must not be mistaken for the managed main session.
    env.pop("MAIN_CLAUDE_SESSION_ID", None)
    # The agent runs with cwd inside the extracted tree, which carries the repo's
    # own `.claude` hooks and has no `.git`. Without this marker the mngr Stop
    # hooks (e.g. the "return to repo root" guard) fire `exit 2` and block the
    # headless agent from ever stopping -- it hangs after finishing its work.
    # This is the same flag those hooks check to skip for proxied subagents.
    env["MNGR_CLAUDE_SUBAGENT_PROXY_CHILD"] = "1"
    return env


def run_streaming_agent(
    prompt: str,
    *,
    cwd: Path,
    log_path: Path,
    model: str,
    append_system_prompt: str,
    header: str,
    timeout_s: int,
    permission_mode: str = "bypassPermissions",
) -> AgentRun:
    """Run a headless ``claude -p`` agent in ``cwd``, streaming its activity to
    ``log_path`` line-by-line so the UI can show live progress while it works.

    ``header`` is written as the first log line (a human-readable "what is
    happening" banner). Returns the agent's final result text and total cost.
    Raises :class:`AgentError` if the process exits non-zero (e.g. timed out).
    """
    argv = [
        "claude", "-p", prompt,
        "--output-format", "stream-json", "--verbose",
        "--model", model,
        "--permission-mode", permission_mode,
        "--append-system-prompt", append_system_prompt,
    ]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        argv,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        env=_agent_env(),
    )
    killer = threading.Timer(timeout_s, proc.kill)
    killer.start()
    result_text, cost = "", 0.0
    try:
        with open(log_path, "w") as log:
            log.write(header.rstrip("\n") + "\n")
            log.flush()
            for raw in proc.stdout or []:
                line = raw.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(ev, dict):
                    continue
                for entry in render_stream_event(ev):
                    log.write(entry + "\n")
                log.flush()
                if ev.get("type") == "result":
                    result_text = ev.get("result") or ""
                    try:
                        cost = float(ev.get("total_cost_usd") or 0.0)
                    except (TypeError, ValueError):
                        cost = 0.0
        proc.wait()
    finally:
        killer.cancel()
    if proc.returncode not in (0, None):
        raise AgentError(f"agent exited {proc.returncode}")
    return AgentRun(text=result_text, cost_usd=cost)
