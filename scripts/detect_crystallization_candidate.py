#!/usr/bin/env python3
"""Stop-hook detector: nudges the main agent to consider crystallizing the turn.

Reads the Claude Code Stop-hook JSON payload from stdin, walks the transcript
backward to the most recent user message, and counts non-read tool_use blocks
in the turn that just finished. When the count crosses a threshold the script
writes a reminder to stderr and exits 2 so the reminder surfaces to the agent.

The detection is intentionally dumb: the main agent applies its own judgement
about whether the turn is actually worth crystallizing.

Suppression rules (in priority order):
1. Worker sub-agent — workers run their own crystallization lifecycle.
2. Latest turn already invoked a crystallized skill successfully.
3. Latest turn is below the tool-call threshold.
4. A lifecycle skill (do-something-new, crystallize-task) was invoked in this
   transcript and no successful ``git commit`` has happened since. The live
   phase is still in progress; the skill itself handles crystallization at the
   commit boundary.
5. We already nudged once for the current commit count — wait for the next
   successful commit before re-arming.

Runtime contract:
- stdin: Claude Code Stop-hook JSON payload (must include ``transcript_path``).
- exit 0: stay silent.
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
    """Load the shared transcript_parsing module from .agents/shared/scripts/.

    The module lives under ``.agents/shared/scripts/`` so any worker script
    (e.g. ``extract_turn.py``) can import it as a sibling and any top-level
    hook can locate it at a fixed shared path. From this top-level hook we
    load it via importlib because the directory is not on the default import
    path.
    """
    workdir = os.environ.get("MNGR_AGENT_WORK_DIR")
    base = Path(workdir) if workdir else Path(__file__).resolve().parent.parent
    module_path = base / ".agents" / "shared" / "scripts" / "transcript_parsing.py"
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
QUALIFYING_CALL_THRESHOLD: int = 8

# Lifecycle skills whose mid-flow presence suppresses the nudge. These skills
# already drive crystallization on their own schedule, so a generic nudge
# during them is pure noise.
LIFECYCLE_SKILL_NAMES: frozenset[str] = frozenset(
    {"do-something-new", "crystallize-task"}
)

# Path (relative to workdir) where we persist nudge state across hook fires
# so we only nudge once per commit window.
NUDGE_STATE_REL_PATH: str = "runtime/.crystallize_nudge_state.json"

# Matches the Bash command of a successful ``git commit`` invocation. We
# only require the literal ``git commit`` token; subcommands like
# ``git commit-tree`` are excluded by the word boundary.
_GIT_COMMIT_PATTERN = re.compile(r"\bgit commit\b")

# Matches a slash-style command marker in a user-message string, e.g.
# ``<command-name>/do-something-new</command-name>``. The leading slash is
# optional so we also catch the un-slashed form some clients emit.
_COMMAND_NAME_PATTERN = re.compile(r"<command-name>/?([\w-]+)</command-name>")

REMINDER_MESSAGE: str = (
    "The turn that just finished used {count} non-read tool calls.\n"
    "\n"
    "Quick check: would repeating this task with new inputs follow a "
    "largely similar process -- same sources, same steps, same criteria, "
    "just different data? Judgement steps in the middle of a flow are "
    "fine; The question is "
    "whether the *process* (or significant parts of it) would repeat recognizably.\n"
    "\n"
    "If no -- the re-run would require entirely new thinking from scratch "
    "-- ignore this reminder.\n"
    "\n"
    "If yes or uncertain: read "
    "`.agents/skills/crystallize-task/references/when-to-crystallize.md`. "
    "That file "
    "contains the decision criteria and common reasoning traps.\n"
    "\n"
    "If the task seems like a potential crystallization candidate, ask the user whether they "
    "expect to ever run it again; if so, you should crystallize it."
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


def _errored_tool_use_ids(events: list[dict[str, Any]]) -> set[str]:
    """Set of tool_use_ids whose tool_result reported is_error=True."""
    out: set[str] = set()
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
            if block.get("is_error") is not True:
                continue
            tool_use_id = block.get("tool_use_id")
            if isinstance(tool_use_id, str):
                out.add(tool_use_id)
    return out


def _find_successful_crystallized_skill_call(
    events: list[dict[str, Any]], skills_root: Path
) -> bool:
    """True if any Skill tool call in the turn targeted a crystallized skill and did not error."""
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

    errored_ids = _errored_tool_use_ids(events)
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


def _last_lifecycle_invocation_index(events: list[dict[str, Any]]) -> int | None:
    """Index of the most recent lifecycle-skill invocation in the transcript.

    Detects two invocation forms:

    - Programmatic: an assistant ``tool_use`` block with ``name: "Skill"`` and
      ``input.skill`` set to a lifecycle skill name. Plugin prefixes
      (``"plugin:do-something-new"``) are stripped before matching.
    - Slash command: a user message whose ``content`` string contains a
      ``<command-name>/<skill></command-name>`` marker.
    """
    last: int | None = None
    for index, event in enumerate(events):
        event_type = event.get("type")
        if event_type == "assistant":
            message = event.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_use" or block.get("name") != "Skill":
                    continue
                tool_input = block.get("input")
                if not isinstance(tool_input, dict):
                    continue
                skill_name = tool_input.get("skill")
                if not isinstance(skill_name, str):
                    continue
                bare = skill_name.split(":", 1)[-1]
                if bare in LIFECYCLE_SKILL_NAMES:
                    last = index
                    break
        elif event_type == "user":
            message = event.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, str):
                continue
            for match in _COMMAND_NAME_PATTERN.finditer(content):
                if match.group(1) in LIFECYCLE_SKILL_NAMES:
                    last = index
                    break
    return last


def _successful_commit_indices(events: list[dict[str, Any]]) -> list[int]:
    """Indices of assistant events containing a successful ``git commit`` Bash call.

    A commit counts as successful when its tool_result is not flagged as an
    error. Multiple commits in a single assistant event collapse to one
    index; we only need the count and the position relative to skill
    invocations.
    """
    errored = _errored_tool_use_ids(events)
    indices: list[int] = []
    for index, event in enumerate(events):
        if event.get("type") != "assistant":
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
            if block.get("type") != "tool_use" or block.get("name") != "Bash":
                continue
            block_id = block.get("id")
            if not isinstance(block_id, str) or block_id in errored:
                continue
            tool_input = block.get("input")
            if not isinstance(tool_input, dict):
                continue
            command = tool_input.get("command")
            if isinstance(command, str) and _GIT_COMMIT_PATTERN.search(command):
                indices.append(index)
                break
    return indices


def _read_nudge_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_nudge_state(path: Path, transcript_path: str, commit_count: int) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"transcript_path": transcript_path, "commit_count": commit_count}
            ),
            encoding="utf-8",
        )
    except OSError:
        # Persisting the nudge state is best-effort; if it fails we just nudge
        # again next turn rather than crashing the hook.
        pass


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


def evaluate(
    payload: dict[str, Any], skills_root: Path, workdir: Path
) -> tuple[bool, str]:
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

    # Suppress while a lifecycle skill flow is mid-run: a lifecycle skill
    # was invoked in this transcript and no successful commit has happened
    # since. The skill drives crystallization itself once the live phase
    # commits.
    commit_indices = _successful_commit_indices(events)
    skill_index = _last_lifecycle_invocation_index(events)
    if skill_index is not None and not any(
        commit_index > skill_index for commit_index in commit_indices
    ):
        return False, ""

    # Suppress if we already nudged for the current commit window. The next
    # successful commit increments the count and re-arms the nudge.
    nudge_state_path = workdir / NUDGE_STATE_REL_PATH
    nudge_state = _read_nudge_state(nudge_state_path)
    last_commit_count = nudge_state.get("commit_count")
    if (
        nudge_state.get("transcript_path") == transcript_path_str
        and isinstance(last_commit_count, int)
        and len(commit_indices) <= last_commit_count
    ):
        return False, ""

    _write_nudge_state(nudge_state_path, transcript_path_str, len(commit_indices))
    return True, REMINDER_MESSAGE.format(count=count)


def _workspace_root() -> Path:
    """Resolve the workspace root for the current agent."""
    workdir = os.environ.get("MNGR_AGENT_WORK_DIR")
    return Path(workdir) if workdir else Path.cwd()


def _skills_root() -> Path:
    """Resolve the shared ``.agents/skills`` directory for the current workspace."""
    return _workspace_root() / ".agents" / "skills"


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

    should_warn, message = evaluate(payload, _skills_root(), _workspace_root())
    if not should_warn:
        return 0
    print(message, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
