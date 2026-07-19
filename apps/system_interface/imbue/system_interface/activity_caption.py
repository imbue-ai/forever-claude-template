"""Compute the user-facing caption for a TOOL_RUNNING activity state.

The activity strip above the chat shows one of three states (IDLE / THINKING /
TOOL_RUNNING; see ``activity_state``). For TOOL_RUNNING we enrich the bare
"Running tool…" with a verb + target derived from the tool call in flight --
"Reading foo.py", "Editing bar.ts", "Searching the web \"...\"".

This lives on the backend (not the frontend) because the label is downstream of
transcript parsing, which is already harness-aware here, and because future
signals may need on-disk artifacts the browser cannot see. The frontend just
renders the string this produces.

Harness split (the caller passes ``is_codex``): each verb owns up to two paths --
one Claude tool, one Codex tool -- kept as identical as possible ("Editing" for
both Claude's Edit and Codex's apply_patch). A tool with no path for its harness
falls through to the generic "Running tool…" bucket. Codex specifics:
- ``exec`` (code mode) is a JS program, not a clean command -> fixed "Running code".
- ``apply_patch`` carries no file field; the path is parsed from the patch header.
- ``web_search`` is surfaced by the parser as a synthetic tool call with a ``query``.
"""

from __future__ import annotations

import json
import re
from typing import Any

_MAX_TARGET_LEN = 60

_GENERIC = "Running tool…"

# --- Claude tool -> verb ---------------------------------------------------
# (Agent / Task are handled separately -> "Delegating to sub-agent…".)
_CLAUDE_VERB_BY_TOOL: dict[str, str] = {
    "Read": "Reading",
    "Edit": "Editing",
    "MultiEdit": "Editing",
    "Write": "Writing",
    "Bash": "Running",
    "Grep": "Searching",
    "Glob": "Searching",
    "Skill": "Loading skill",
    "ToolSearch": "Loading tool",
    "WebSearch": "Searching the web",
    "WebFetch": "Fetching page",
    "LSP": "Querying language server",
    "NotebookEdit": "Editing notebook",
    "Monitor": "Monitoring",
    "SendMessage": "Sending message",
}

# --- Codex tool -> verb ----------------------------------------------------
# Labels intentionally mirror the Claude verbs where both harnesses have the
# capability (Editing, Searching the web, Running). ``exec`` (code mode) is handled
# separately below; ``apply_patch`` is here for its verb but its target is parsed
# specially (from the patch header, not a JSON field).
_CODEX_VERB_BY_TOOL: dict[str, str] = {
    "shell": "Running",
    "shell_command": "Running",
    "exec_command": "Running",
    "local_shell": "Running",
    "apply_patch": "Editing",
    "view_image": "Viewing image",
    "web_search": "Searching the web",
}

# Codex code-mode shell tool: a ``custom_tool_call`` whose input is a JavaScript
# program (e.g. ``tools.exec_command({cmd:"..."})``), not a tidy command. We show
# a fixed label rather than trying to parse the JS.
_CODEX_CODE_EXEC_TOOL = "exec"
_CODEX_CODE_EXEC_LABEL = "Running code"

# Codex's experimental multi-agent spawn tools -> "Delegating to sub-agent…".
_CODEX_SPAWN_TOOL_PREFIX = "spawn_agent"

_MCP_PREFIX = "mcp__"


def _shorten(s: str, max_len: int = _MAX_TARGET_LEN) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return (s[: max_len - 1] + "…") if len(s) > max_len else s


def _basename(p: str) -> str:
    idx = max(p.rfind("/"), p.rfind("\\"))
    return p[idx + 1 :] if idx >= 0 else p


def _parse_params(input_preview: str) -> dict[str, Any]:
    """A tool call's ``input_preview`` is the JSON ``arguments`` string (function
    calls) -- parse it to a dict. Non-JSON inputs (e.g. an apply_patch body) return
    an empty dict; their handlers read the raw string instead."""
    try:
        parsed = json.loads(input_preview)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _first_str(params: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = params.get(key)
        if isinstance(value, str) and value != "":
            return value
    return None


# apply_patch has no file field; the path lives in the patch envelope header,
# e.g. ``*** Update File: path/to/f.py`` (also Add/Delete File, Move to).
_APPLY_PATCH_HEADER = re.compile(r"\*\*\*\s+(?:Add|Update|Delete) File:\s*(.+)", re.IGNORECASE)


def _apply_patch_target(input_preview: str) -> str | None:
    # Freeform apply_patch: input_preview is the raw patch text. Function-mode:
    # it is ``{"input": "<patch text>"}`` -- unwrap that first.
    text = input_preview
    params = _parse_params(input_preview)
    inner = params.get("input")
    if isinstance(inner, str):
        text = inner
    match = _APPLY_PATCH_HEADER.search(text)
    if match is None:
        return None
    return _basename(match.group(1).strip())


def _claude_target(tool_name: str, params: dict[str, Any]) -> str | None:
    # Bash: prefer the agent-supplied human ``description`` over the raw command.
    if tool_name == "Bash":
        description = _first_str(params, "description")
        if description is not None:
            return _shorten(description)
        command = _first_str(params, "command")
        return _shorten(command) if command is not None else None

    file_path = _first_str(params, "file_path", "path")
    if file_path is not None:
        return _basename(file_path)
    url = _first_str(params, "url")
    if url is not None:
        return _shorten(url)
    command = _first_str(params, "command")
    if command is not None:
        return _shorten(command)
    for quoted_key in ("pattern", "query"):
        value = _first_str(params, quoted_key)
        if value is not None:
            return f'"{_shorten(value)}"'
    other = _first_str(params, "skill", "description")
    return _shorten(other) if other is not None else None


def _codex_target(tool_name: str, params: dict[str, Any], input_preview: str) -> str | None:
    if tool_name == "apply_patch":
        return _apply_patch_target(input_preview)
    if tool_name == "view_image":
        path = _first_str(params, "path")
        return _basename(path) if path is not None else None
    if tool_name == "web_search":
        query = _first_str(params, "query")
        return f'"{_shorten(query)}"' if query is not None else None
    # shell family: the command lives in ``cmd`` (exec_command) or ``command``
    # (shell / shell_command / hosted local_shell, which may be a list).
    cmd = _first_str(params, "cmd", "command")
    if cmd is not None:
        return _shorten(cmd)
    command_list = params.get("command")
    if isinstance(command_list, list) and command_list:
        return _shorten(" ".join(str(part) for part in command_list))
    return None


def _label_for_mcp(tool_name: str) -> str | None:
    """``mcp__<server>__<tool>`` -> "Running <tool with spaces>". Same convention
    for Claude and Codex."""
    if not tool_name.startswith(_MCP_PREFIX):
        return None
    last_sep = tool_name.rfind("__")
    if last_sep <= len(_MCP_PREFIX) - 1:
        return None
    tool_part = tool_name[last_sep + 2 :]
    if tool_part == "":
        return None
    return f"Running {tool_part.replace('_', ' ')}"


def caption_for_tool_call(tool_name: str, input_preview: str, *, is_codex: bool) -> str:
    """Return the TOOL_RUNNING caption for a single in-flight tool call.

    ``input_preview`` is the tool call's raw argument string as the parser stores
    it (the JSON ``arguments`` for function calls, or the raw ``input`` for custom
    tool calls). Always returns a non-empty string (falls back to "Running tool…").
    """
    # Delegation short-circuits, both harnesses.
    if tool_name in ("Agent", "Task") or tool_name.startswith(_CODEX_SPAWN_TOOL_PREFIX):
        return "Delegating to sub-agent…"

    if is_codex and tool_name == _CODEX_CODE_EXEC_TOOL:
        return _CODEX_CODE_EXEC_LABEL

    verb_by_tool = _CODEX_VERB_BY_TOOL if is_codex else _CLAUDE_VERB_BY_TOOL
    verb = verb_by_tool.get(tool_name)

    params = _parse_params(input_preview)
    target = (
        _codex_target(tool_name, params, input_preview) if is_codex else _claude_target(tool_name, params)
    )

    if verb is not None and target is not None:
        return f"{verb} {target}"
    if verb is not None:
        return f"{verb}…"

    mcp_label = _label_for_mcp(tool_name)
    if mcp_label is not None:
        return mcp_label

    # Last resort: an unmapped tool that still exposed a recognizable target.
    if target is not None:
        return f"Running {target}"
    return _GENERIC
