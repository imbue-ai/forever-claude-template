#!/usr/bin/env python3
"""Stop-hook detector: nudges the main agent to consider crystallizing the turn.

Reads the Claude Code Stop-hook JSON payload from stdin, walks the transcript
backward to the most recent user message, and counts non-read tool_use blocks
in the turn that just finished. When the count crosses a threshold the script
writes a reminder to stderr and exits 2 so the reminder surfaces to the agent.

The detection is intentionally dumb: the main agent applies its own judgement
about whether the turn is actually worth crystallizing.

Runtime contract:
- stdin: Claude Code Stop-hook JSON payload (must include ``transcript_path``).
- exit 0: stay silent (nothing to do, or the turn was already handled by an
  existing crystallized skill, or we are inside a worker sub-agent).
- exit 2: print the reminder to stderr; Claude Code surfaces it to the agent.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


def _load_transcript_parsing() -> Any:
    """Load the shared transcript_parsing module from the crystallize-task skill.

    The module lives under ``.agents/skills/crystallize-task/scripts/`` so the
    PEP 723 worker script (``extract_turn.py``) can import it as a sibling.
    From this top-level hook we load it via importlib because it is not on the
    default import path.
    """
    workdir = os.environ.get("MNGR_AGENT_WORK_DIR")
    base = Path(workdir) if workdir else Path(__file__).resolve().parent.parent
    module_path = base / ".agents" / "skills" / "crystallize-task" / "scripts" / "transcript_parsing.py"
    spec = importlib.util.spec_from_file_location("transcript_parsing", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load transcript_parsing from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_transcript_parsing = _load_transcript_parsing()
iter_transcript = _transcript_parsing.iter_transcript
is_user_tool_result_carrier = _transcript_parsing.is_user_tool_result_carrier

# Tool names that count as "pure reads" and are excluded from the tally. The
# spec (concise.md) is explicit about these three names.
READ_ONLY_TOOLS: frozenset[str] = frozenset({"Read", "Grep", "Glob"})

# Threshold at which the hook emits a reminder.
QUALIFYING_CALL_THRESHOLD: int = 5

REMINDER_MESSAGE: str = (
    "The turn that just finished used {count} non-read tool calls. "
    "Consider whether any portion of the work is worth crystallizing into "
    "a reusable skill via `crystallize-task`. This includes sub-processes "
    "within a larger task, not just the task as a whole. In particular, if "
    "you learned how to do something -- through research, debugging, or "
    "experimentation -- that seems likely to be useful in the future, and "
    "the process is mostly deterministic, that is a strong signal to "
    "crystallize it. "
    "If the entire turn was pure one-off work with nothing reusable, "
    "ignore this reminder."
)


def _iter_assistant_tool_uses(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collect all tool_use content blocks from assistant events in the turn."""
    tool_uses: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") != "assistant":
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tool_uses.append(block)
    return tool_uses


def _count_qualifying(tool_uses: list[dict[str, Any]]) -> int:
    return sum(1 for block in tool_uses if block.get("name") not in READ_ONLY_TOOLS)


_FRONTMATTER_PATTERN = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
_CRYSTALLIZED_PATTERN = re.compile(r"^\s*crystallized\s*:\s*true\b", re.MULTILINE)


def _skill_is_crystallized(skill_md_path: Path) -> bool:
    """Return True if the SKILL.md frontmatter declares metadata.crystallized: true.

    Uses regex rather than a YAML parser so the hook has zero runtime deps and
    starts fast. The pattern matches ``crystallized: true`` under any indent,
    which is good enough: the field name is unique enough that a false positive
    would require someone to stash the literal string in a non-metadata block.
    """
    if not skill_md_path.is_file():
        return False
    text = skill_md_path.read_text(encoding="utf-8", errors="replace")
    match = _FRONTMATTER_PATTERN.match(text)
    if match is None:
        return False
    return bool(_CRYSTALLIZED_PATTERN.search(match.group(1)))


def _find_successful_crystallized_skill_call(
    events: list[dict[str, Any]], skills_root: Path
) -> bool:
    """True if any Skill tool call in the turn targeted a crystallized skill and did not error."""
    # Build a map of tool_use_id -> skill name so we can cross-reference results.
    skill_calls: dict[str, str] = {}
    for block in _iter_assistant_tool_uses(events):
        if block.get("name") != "Skill":
            continue
        block_id = block.get("id")
        tool_input = block.get("input")
        if not isinstance(block_id, str) or not isinstance(tool_input, dict):
            continue
        skill_name = tool_input.get("skill")
        if isinstance(skill_name, str):
            skill_calls[block_id] = skill_name
    if not skill_calls:
        return False

    errored_ids: set[str] = set()
    for event in events:
        if event.get("type") != "user":
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_result":
                continue
            if block.get("is_error") is True:
                tool_use_id = block.get("tool_use_id")
                if isinstance(tool_use_id, str):
                    errored_ids.add(tool_use_id)

    for block_id, skill_name in skill_calls.items():
        if block_id in errored_ids:
            continue
        # Strip any plugin prefix (e.g. "foo:bar") -- plugin-namespaced skills
        # still resolve to a <name>/SKILL.md directory.
        bare_name = skill_name.split(":", 1)[-1]
        skill_md = skills_root / bare_name / "SKILL.md"
        if _skill_is_crystallized(skill_md):
            return True
    return False


def _latest_response_boundary(events: list[dict[str, Any]]) -> int | None:
    """Index of the event that prompted the agent's most recent response.

    This is the most recent ``type: user`` event that is NOT a tool_result
    carrier. Unlike ``last_user_message_index`` in ``transcript_parsing``,
    this deliberately includes ``isMeta: true`` events (e.g. Stop-hook
    re-injections): those also prompt a fresh assistant response, so the
    tool-use count for "the turn that just finished" should reset at them.
    """
    for index in range(len(events) - 1, -1, -1):
        event = events[index]
        if event.get("type") != "user":
            continue
        if is_user_tool_result_carrier(event):
            continue
        return index
    return None


def evaluate(payload: dict[str, Any], skills_root: Path) -> tuple[bool, str]:
    """Detection entry point.

    Returns ``(should_warn, message)``. ``should_warn == False`` means the
    caller should stay silent; ``message`` is meaningful only when the first
    element is True.

    "The turn that just finished" is the agent's most recent response --
    everything after the most recent non-tool-result user event (including
    Stop-hook meta injections). If the agent replied without tools, the
    count is zero and the hook stays silent, which is what we want when the
    Stop hook re-fires after a tool-free acknowledgement.
    """
    transcript_path_str = payload.get("transcript_path")
    if not isinstance(transcript_path_str, str):
        return False, ""
    transcript_path = Path(transcript_path_str)
    if not transcript_path.is_file():
        return False, ""

    events = iter_transcript(transcript_path)
    boundary = _latest_response_boundary(events)
    turn_events = events if boundary is None else events[boundary + 1 :]
    if not turn_events:
        return False, ""

    if _find_successful_crystallized_skill_call(turn_events, skills_root):
        return False, ""

    count = _count_qualifying(_iter_assistant_tool_uses(turn_events))
    if count < QUALIFYING_CALL_THRESHOLD:
        return False, ""
    return True, REMINDER_MESSAGE.format(count=count)


def _skills_root() -> Path:
    """Resolve the shared ``.agents/skills`` directory for the current workspace."""
    workdir = os.environ.get("MNGR_AGENT_WORK_DIR")
    base = Path(workdir) if workdir else Path.cwd()
    return base / ".agents" / "skills"


def main() -> int:
    if os.environ.get("MNGR_AGENT_ROLE") == "worker":
        # Workers run their own crystallization lifecycle; don't nudge them.
        return 0

    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, dict):
        return 0

    should_warn, message = evaluate(payload, _skills_root())
    if not should_warn:
        return 0
    print(message, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
