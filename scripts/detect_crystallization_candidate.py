#!/usr/bin/env python3
"""Stop-hook detector: nudges the main agent to consider crystallizing the turn.

Reads the lead agent's common transcript (the agent-agnostic event log that
``mngr`` maintains for every claude / codex agent at
``$MNGR_AGENT_STATE_DIR/events/claude/common_transcript/events.jsonl``),
walks the events backward to the most recent user message, and counts
non-read tool calls in the turn that just finished. When the count crosses
a threshold the script writes a reminder to stderr and exits 2 so the
reminder surfaces to the agent.

The detection is intentionally dumb: the main agent applies its own
judgement about whether the turn is actually worth crystallizing.

Why the common transcript and not the raw Claude transcript path the Stop
hook payload hands us: the common transcript is what every other tool in
this codebase reads, so the hook isn't a special-case consumer of
Claude-specific JSONL anymore. The catch is that the common-transcript
converter normally polls on a 5s interval, so the just-finished turn may
not yet be flushed when we read. We solve that by invoking the converter's
``--single-pass`` mode synchronously at hook entry.

Suppression rules (in priority order):
1. Worker sub-agent — workers run their own crystallization lifecycle.
2. Latest turn already invoked a crystallized skill successfully.
3. Latest turn is below the tool-call threshold.
4. A lifecycle skill (do-something-new, crystallize-task) was invoked in
   this transcript and no successful ``git commit`` has happened since.
   The live phase is still in progress; the skill itself handles
   crystallization at the commit boundary.
5. We already nudged once for the current commit count — wait for the
   next successful commit before re-arming.

Runtime contract:
- stdin: Claude Code Stop-hook JSON payload (consumed for validity, not
  for transcript path).
- env: ``MNGR_AGENT_STATE_DIR`` resolves the common transcript location.
  Unset (e.g. standalone Claude outside ``mngr``) → silent no-op.
- exit 0: stay silent.
- exit 2: print the reminder to stderr; Claude Code surfaces it to the
  agent.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


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

# Matches the Bash command of a successful ``git commit`` invocation. The
# trailing negative lookahead excludes subcommands like ``git commit-tree``
# and plurals like ``git commits`` -- a plain word boundary is insufficient
# because ``\b`` also matches between ``commit`` and ``-``.
_GIT_COMMIT_PATTERN = re.compile(r"\bgit commit(?![-\w])")

# Matches a slash-style command marker in a user-message string, e.g.
# ``<command-name>/do-something-new</command-name>``. The leading slash is
# optional so we also catch the un-slashed form some clients emit.
_COMMAND_NAME_PATTERN = re.compile(r"<command-name>/?([\w-]+)</command-name>")

# Where the common-transcript converter script lives within an agent's
# state dir (set up by mngr_claude's resource installer).
_COMMON_TRANSCRIPT_SCRIPT_REL = Path("commands/common_transcript.sh")

# Where the converted common-transcript events.jsonl lives.
_COMMON_TRANSCRIPT_EVENTS_REL = Path("events/claude/common_transcript/events.jsonl")

REMINDER_MESSAGE: str = (
    "The turn that just finished used {count} non-read tool calls{interrupt_note}.\n"
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

# Claude Code's post-interrupt resume bookkeeping. When ``claude --resume``
# reloads a session whose previous turn was cut off (e.g. by the stop button's
# ``mngr start --restart``), the framework injects an ``isMeta`` user message
# with exactly this text to close the dangling turn. The common-transcript
# converter reclassifies that isMeta user message into a ``tool_result`` event
# with ``tool_name == "meta"`` and the text in ``output`` (see the converter's
# isMeta branch). This literal mirrors ``_RESUME_CONTINUATION_TEXT`` in the
# system_interface ``session_parser``: both are independent consumers of the
# same Claude Code behavior, and there is no common module either side can
# import, so the string is duplicated by design.
RESUME_CONTINUATION_TEXT = "Continue from where you left off."


def is_resume_continuation_marker(event: dict[str, Any]) -> bool:
    """True if ``event`` is Claude Code's post-interrupt resume marker.

    In the common transcript the marker is a ``tool_result`` event with
    ``tool_name == "meta"`` whose ``output`` is exactly the resume-continuation
    sentinel (see ``RESUME_CONTINUATION_TEXT``). A Stop-hook re-injection is
    also a ``meta`` tool_result but carries different output, so gating on the
    exact text distinguishes the two.
    """
    if event.get("type") != "tool_result" or event.get("tool_name") != "meta":
        return False
    output = event.get("output")
    return isinstance(output, str) and output.strip() == RESUME_CONTINUATION_TEXT


def _interrupt_note(interrupt_count: int) -> str:
    """Concise parenthetical for a turn that spanned stop-button interrupts.

    Empty when the turn was not interrupted. Otherwise it flags that the count
    spans pre- and post-interrupt work and gives the escape hatch for the case
    where the resumed work was actually an unrelated task.
    """
    if interrupt_count <= 0:
        return ""
    times = "once" if interrupt_count == 1 else f"{interrupt_count} times"
    return (
        f" (interrupted by the stop button {times}; the count spans the work "
        "before and after -- ignore this if those were unrelated tasks)"
    )


def _read_common_transcript(path: Path) -> list[dict[str, Any]]:
    """Return common-transcript events as a list; tolerates malformed lines."""
    events: list[dict[str, Any]] = []
    try:
        handle = path.open(encoding="utf-8")
    except OSError:
        return events
    with handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                events.append(parsed)
    return events


def _flush_common_transcript(state_dir: Path) -> None:
    """Synchronously run the common-transcript converter so events.jsonl
    catches up with the just-finished turn. Best-effort; a flush failure
    is logged-and-skipped, not fatal."""
    script = state_dir / _COMMON_TRANSCRIPT_SCRIPT_REL
    if not script.is_file():
        return
    try:
        result = subprocess.run(
            [str(script), "--single-pass"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return
    if result.returncode != 0:
        # The hook still runs against whatever the 5s poller had time to
        # write before we got here -- worst case we miss the most recent
        # tool calls, which only causes an under-count (silent), never an
        # over-count (false alarm).
        return


def _parse_input_preview(preview: Any) -> dict[str, Any] | None:
    """Best-effort decode of a common-transcript ``input_preview`` string.

    The converter JSON-encodes the tool input and truncates to 200 chars
    (with a literal ``...`` suffix). Short inputs round-trip cleanly;
    long ones fail to parse and the caller falls back to substring
    matching on the raw preview.
    """
    if not isinstance(preview, str):
        return None
    candidate = preview[:-3] if preview.endswith("...") else preview
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _iter_tool_calls(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collect all tool_calls entries from assistant_message events in order."""
    tool_calls: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") != "assistant_message":
            continue
        for call in event.get("tool_calls") or ():
            if isinstance(call, dict):
                tool_calls.append(call)
    return tool_calls


def _count_qualifying(tool_calls: list[dict[str, Any]]) -> int:
    return sum(1 for call in tool_calls if call.get("tool_name") not in READ_ONLY_TOOLS)


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


def _errored_tool_call_ids(events: list[dict[str, Any]]) -> set[str]:
    """Set of tool_call_ids whose tool_result reported is_error=True."""
    out: set[str] = set()
    for event in events:
        if event.get("type") != "tool_result":
            continue
        if event.get("is_error") is not True:
            continue
        call_id = event.get("tool_call_id")
        if isinstance(call_id, str):
            out.add(call_id)
    return out


def _skill_input_name(call: dict[str, Any]) -> str | None:
    """Extract ``input.skill`` from a Skill tool call's input_preview, or None."""
    parsed = _parse_input_preview(call.get("input_preview"))
    if parsed is None:
        return None
    skill = parsed.get("skill")
    return skill if isinstance(skill, str) else None


def _bash_input_matches_git_commit(call: dict[str, Any]) -> bool:
    """True iff a Bash tool call invokes ``git commit``.

    Prefers parsing the input_preview JSON and checking the ``command``
    field exactly (avoids false positives from words like 'commit' in the
    description). If JSON parsing fails -- usually because the preview
    was truncated -- falls back to substring matching on the raw preview,
    which is conservative but rarely false-positives in practice.
    """
    preview = call.get("input_preview")
    parsed = _parse_input_preview(preview)
    if parsed is not None:
        command = parsed.get("command")
        if isinstance(command, str):
            return bool(_GIT_COMMIT_PATTERN.search(command))
    if isinstance(preview, str):
        return bool(_GIT_COMMIT_PATTERN.search(preview))
    return False


def _find_successful_crystallized_skill_call(
    events: list[dict[str, Any]], skills_root: Path
) -> bool:
    """True if any Skill tool call in the turn targeted a crystallized skill and did not error."""
    skill_calls: dict[str, str] = {}
    for call in _iter_tool_calls(events):
        if call.get("tool_name") != "Skill":
            continue
        call_id = call.get("tool_call_id")
        if not isinstance(call_id, str):
            continue
        skill_name = _skill_input_name(call)
        if skill_name is not None:
            skill_calls[call_id] = skill_name
    if not skill_calls:
        return False

    errored = _errored_tool_call_ids(events)
    for call_id, skill_name in skill_calls.items():
        if call_id in errored:
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

    - Programmatic: an ``assistant_message`` event whose ``tool_calls``
      includes a ``Skill`` call resolving to a lifecycle skill name
      (plugin prefixes stripped).
    - Slash command: a ``user_message`` event whose ``content`` string
      contains a ``<command-name>/<skill></command-name>`` marker.
    """
    last: int | None = None
    for index, event in enumerate(events):
        ev_type = event.get("type")
        if ev_type == "assistant_message":
            for call in event.get("tool_calls") or ():
                if not isinstance(call, dict) or call.get("tool_name") != "Skill":
                    continue
                skill_name = _skill_input_name(call)
                if skill_name is None:
                    continue
                bare = skill_name.split(":", 1)[-1]
                if bare in LIFECYCLE_SKILL_NAMES:
                    last = index
                    break
        elif ev_type == "user_message":
            content = event.get("content")
            if not isinstance(content, str):
                continue
            for match in _COMMAND_NAME_PATTERN.finditer(content):
                if match.group(1) in LIFECYCLE_SKILL_NAMES:
                    last = index
                    break
    return last


def _successful_commit_indices(events: list[dict[str, Any]]) -> list[int]:
    """Indices of assistant_message events containing a successful ``git commit`` Bash call.

    A commit counts as successful when its tool_result is not flagged as
    an error. Multiple commits in a single assistant event collapse to
    one index; we only need the count and the position relative to
    skill invocations.
    """
    errored = _errored_tool_call_ids(events)
    indices: list[int] = []
    for index, event in enumerate(events):
        if event.get("type") != "assistant_message":
            continue
        for call in event.get("tool_calls") or ():
            if not isinstance(call, dict) or call.get("tool_name") != "Bash":
                continue
            call_id = call.get("tool_call_id")
            if not isinstance(call_id, str) or call_id in errored:
                continue
            if _bash_input_matches_git_commit(call):
                indices.append(index)
                break
    return indices


def _read_nudge_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_nudge_state(path: Path, transcript_id: str, commit_count: int) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"transcript_id": transcript_id, "commit_count": commit_count}
            ),
            encoding="utf-8",
        )
    except OSError:
        # Persisting the nudge state is best-effort; if it fails we just nudge
        # again next turn rather than crashing the hook.
        pass


def _previous_response_boundary(
    events: list[dict[str, Any]], before: int
) -> int | None:
    """Index of the most recent response boundary before ``before``.

    A response boundary is either:
    - A ``user_message`` (a real user input), or
    - A ``tool_result`` event with ``tool_name == "meta"`` (Claude Code's
      isMeta injections -- Stop-hook re-injections and post-interrupt resume
      markers alike -- which the common-transcript converter reclassifies from
      isMeta user events). Both kinds prompt a fresh assistant response, so the
      tool-call count for "the turn that just finished" resets at them.

    The backward scan starts at ``before - 1``; pass ``len(events)`` to scan
    the whole transcript. Used both for the latest boundary and to fold
    interrupted-and-resumed turns.
    """
    for index in range(before - 1, -1, -1):
        event = events[index]
        ev_type = event.get("type")
        if ev_type == "user_message":
            return index
        if ev_type == "tool_result" and event.get("tool_name") == "meta":
            return index
    return None


def _latest_response_boundary(events: list[dict[str, Any]]) -> int | None:
    """Index of the event that prompted the agent's most recent response."""
    return _previous_response_boundary(events, len(events))


def _logical_turn_start(events: list[dict[str, Any]]) -> int | None:
    """Index of the event that begins the logical turn that just finished.

    Starts from ``_latest_response_boundary`` and folds backward across Claude
    Code's post-interrupt resume markers. When the stop button interrupts a
    turn, ``claude --resume`` injects a resume-continuation marker before the
    user's next message; that next message continues the interrupted work
    rather than starting a fresh turn. Folding past the marker makes the
    pre-interrupt tool calls count toward the same turn. The loop collapses
    multiple interrupts within one logical turn.

    A Stop-hook re-injection (a ``meta`` tool_result with different output) is
    NOT folded: it still resets the count, preserving per-response semantics.
    """
    boundary = _latest_response_boundary(events)
    if boundary is None:
        return None
    while True:
        previous = _previous_response_boundary(events, boundary)
        if previous is None or not is_resume_continuation_marker(events[previous]):
            return boundary
        # events[previous] is the framework's resume marker, so the user
        # message at `boundary` continues an interrupted turn. Fold back to
        # the boundary that started that interrupted turn.
        before_marker = _previous_response_boundary(events, previous)
        if before_marker is None:
            return boundary
        boundary = before_marker


def evaluate(
    events: list[dict[str, Any]],
    skills_root: Path,
    workdir: Path,
    transcript_id: str,
) -> tuple[bool, str]:
    """Detection entry point.

    Returns ``(should_warn, message)``. ``should_warn == False`` means
    the caller should stay silent; ``message`` is meaningful only when
    the first element is True.

    "The turn that just finished" is the agent's most recent response --
    everything after the most recent ``user_message`` or
    ``tool_result(tool_name="meta")`` event, with one exception: a turn the
    stop button interrupted and the user resumed is folded back into one turn
    (see ``_logical_turn_start``). If the agent replied without tools, the
    count is zero and the hook stays silent, which is what we want when the
    Stop hook re-fires after a tool-free acknowledgement.

    ``transcript_id`` is the stable identifier under which we persist
    the nudge state -- typically the agent's state dir. When it changes
    (a different agent / wiped state) the suppression cache is treated
    as stale and the nudge re-arms.
    """
    if not events:
        return False, ""

    boundary = _logical_turn_start(events)
    turn_events = events if boundary is None else events[boundary + 1 :]
    if not turn_events:
        return False, ""

    if _find_successful_crystallized_skill_call(turn_events, skills_root):
        return False, ""

    count = _count_qualifying(_iter_tool_calls(turn_events))
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
        nudge_state.get("transcript_id") == transcript_id
        and isinstance(last_commit_count, int)
        and len(commit_indices) <= last_commit_count
    ):
        return False, ""

    _write_nudge_state(nudge_state_path, transcript_id, len(commit_indices))
    interrupt_count = sum(
        1 for event in turn_events if is_resume_continuation_marker(event)
    )
    return True, REMINDER_MESSAGE.format(
        count=count, interrupt_note=_interrupt_note(interrupt_count)
    )


def _workspace_root() -> Path:
    """Resolve the workspace root for the current agent."""
    workdir = os.environ.get("MNGR_AGENT_WORK_DIR")
    return Path(workdir) if workdir else Path.cwd()


def _skills_root() -> Path:
    """Resolve the shared ``.agents/skills`` directory for the current workspace."""
    return _workspace_root() / ".agents" / "skills"


def _state_dir() -> Path | None:
    """Resolve the current agent's state dir, or None outside mngr."""
    state = os.environ.get("MNGR_AGENT_STATE_DIR")
    return Path(state) if state else None


def main() -> int:
    if os.environ.get("MNGR_AGENT_ROLE") == "worker":
        # Workers run their own crystallization lifecycle; don't nudge them.
        return 0

    raw = sys.stdin.read()
    if raw.strip():
        try:
            json.loads(raw)
        except json.JSONDecodeError:
            return 0

    state_dir = _state_dir()
    if state_dir is None:
        # Standalone Claude (no mngr) -- no common transcript to read.
        return 0
    events_path = state_dir / _COMMON_TRANSCRIPT_EVENTS_REL
    if not events_path.is_file():
        return 0

    _flush_common_transcript(state_dir)
    events = _read_common_transcript(events_path)

    should_warn, message = evaluate(
        events, _skills_root(), _workspace_root(), str(state_dir)
    )
    if not should_warn:
        return 0
    print(message, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
