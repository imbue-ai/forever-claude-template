"""Detect whether the chat agent already received `/welcome` and resend if not.

Invoked from the auth-success chokepoint in `claude_auth_endpoints` so a
mind whose initial `/welcome` failed for lack of credentials gets the
greeting once auth recovers.

The welcome skill's opening message text is read at runtime from
`.agents/skills/welcome/SKILL.md`, so this helper and the skill stay in
sync without manual edits.

Whether the welcome was already delivered is decided from the agent's
parsed session transcript, not its live tmux pane: the pane is cleared
and redrawn across `claude --resume` restarts and auth churn, so a
pane scan would miss a welcome that genuinely was shown and resend a
duplicate. The transcript JSONL is the durable record -- if any
assistant turn there rendered the welcome opening line, the welcome has
been delivered and must not be resent.

Side-effecting dependencies (transcript reading and agent message
dispatch) are exposed as module-level callables so tests rebind them
rather than relying on `unittest.mock`.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path

from loguru import logger as _loguru_logger

from imbue.system_interface.agent_discovery import discover_agents
from imbue.system_interface.agent_discovery import send_message
from imbue.system_interface.session_watcher import AgentSessionWatcher

logger = _loguru_logger

_WELCOME_SKILL_RELATIVE_PATH = Path(".agents/skills/welcome/SKILL.md")
_WORK_DIR_ENV_VAR = "MNGR_AGENT_WORK_DIR"
_FRONTMATTER_DELIMITER = "---"
_HEADER_LINE_REGEX = re.compile(r"^#{1,6}\s+.+$", re.MULTILINE)
_WELCOME_COMMAND = "/welcome"


class WelcomeResendError(RuntimeError):
    """Raised when the welcome skill cannot be parsed for its opening line."""


TranscriptReadFn = Callable[[str], "str | None"]
MessageSendFn = Callable[[str, str], bool]


def _strip_frontmatter(body: str) -> str:
    """Drop YAML frontmatter (between leading `---` lines) from a markdown doc."""
    lines = body.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_DELIMITER:
        return body
    for end_index in range(1, len(lines)):
        if lines[end_index].strip() == _FRONTMATTER_DELIMITER:
            return "\n".join(lines[end_index + 1 :])
    return body


def _extract_first_message_header(skill_body: str) -> str | None:
    """Return the first markdown header that appears inside a verbatim block.

    The welcome skill wraps its message in a pair of `---` separators. The
    actual greeting starts with a `###` header on the first non-empty
    line of that block. Walking through every header in the document and
    taking the first one that appears after a `---` separator handles
    that layout without hard-coding which skill format we're parsing.
    """
    inside_block = False
    for line in skill_body.splitlines():
        stripped = line.strip()
        if stripped == _FRONTMATTER_DELIMITER:
            inside_block = not inside_block
            continue
        if inside_block and _HEADER_LINE_REGEX.match(line):
            return line.strip()
    return None


def _default_skill_path() -> Path:
    """Resolve the welcome skill path against the mind's work dir.

    The workspace server is not guaranteed to be launched with its CWD set
    to the mind's work dir, so a bare relative path would silently miss in
    production (read_text raises FileNotFoundError, the OSError branch in
    `check_and_resend_welcome` swallows it, and the welcome never resends).
    Anchoring on MNGR_AGENT_WORK_DIR -- the same env var
    `agent_manager._resolve_observe_cwd` uses -- pins the lookup to the
    correct project root regardless of CWD. Falls back to the bare relative
    path when the env var is unset.
    """
    work_dir = os.environ.get(_WORK_DIR_ENV_VAR, "")
    if work_dir:
        return Path(work_dir) / _WELCOME_SKILL_RELATIVE_PATH
    return _WELCOME_SKILL_RELATIVE_PATH


def read_welcome_opening_line(skill_path: Path | None = None) -> str:
    """Read the welcome skill markdown and return the opening line of the message.

    Falls back to scanning the whole body if no separator-wrapped verbatim
    block is present, in case the skill layout changes in a future
    revision.
    """
    path = skill_path or _default_skill_path()
    text = path.read_text()
    body = _strip_frontmatter(text)
    header = _extract_first_message_header(body)
    if header is not None:
        return header
    match = _HEADER_LINE_REGEX.search(body)
    if match is not None:
        return match.group(0).strip()
    raise WelcomeResendError(f"Could not find a verbatim opening line in welcome skill at {path}")


def _default_read_assistant_transcript(agent_name: str) -> str | None:
    """Return the concatenated text of every assistant turn in the agent's transcript.

    Resolves the agent's session files the same way the `/events` endpoint
    does (via `AgentSessionWatcher`) and joins the `assistant_message`
    text. Only assistant turns are included: the `/welcome` skill
    expansion is a *user* message that also contains the welcome text
    verbatim, so including user turns would always look like a delivered
    welcome. Returns None when the agent or its transcript cannot be found
    so the caller treats the welcome as not-yet-delivered.
    """
    matching = [agent for agent in discover_agents() if agent.name == agent_name]
    if not matching:
        logger.warning("Agent {} not found while checking welcome transcript", agent_name)
        return None
    agent = matching[0]
    watcher = AgentSessionWatcher(
        agent_id=agent.id,
        agent_state_dir=agent.agent_state_dir,
        claude_config_dir=agent.claude_config_dir,
        on_events=lambda _agent_id, _events: None,
    )
    events = watcher.get_all_events()
    assistant_texts = [
        event.get("text", "") for event in events if event.get("type") == "assistant_message"
    ]
    return "\n".join(assistant_texts)


# Injectable module-level dependencies. Production code uses the defaults
# below; tests rebind these (welcome_resend.read_assistant_transcript = fake)
# instead of using `unittest.mock`.
read_assistant_transcript: TranscriptReadFn = _default_read_assistant_transcript
send_message_fn: MessageSendFn = send_message


def _transcript_shows_welcome(transcript: str | None, opening_line: str) -> bool:
    """Treat a missing/empty transcript as 'welcome absent' so we resend.

    A fresh mind whose agent has not produced any assistant turn yet is
    fine to (re-)welcome. The opening line only ever appears in an
    assistant turn that actually rendered the greeting -- auth-error
    turns ("Not logged in ...") never contain it -- so a substring match
    is a reliable "welcome was delivered" signal.
    """
    if not transcript:
        return False
    return opening_line in transcript


def check_and_resend_welcome(agent_name: str, skill_path: Path | None = None) -> bool:
    """If the agent's transcript lacks the welcome opening line, dispatch `/welcome`.

    Returns True when a resend was issued, False when the transcript
    already shows the welcome (no-op).
    """
    try:
        opening_line = read_welcome_opening_line(skill_path)
    except (OSError, WelcomeResendError) as e:
        logger.warning("Could not read welcome skill opening line: {}", e)
        return False

    transcript = read_assistant_transcript(agent_name)
    if _transcript_shows_welcome(transcript, opening_line):
        logger.debug("Agent {} transcript already shows welcome; skipping resend", agent_name)
        return False

    logger.info("Resending /welcome to agent {} (transcript missing opening line)", agent_name)
    sent = send_message_fn(agent_name, _WELCOME_COMMAND)
    if not sent:
        logger.warning("Failed to dispatch /welcome to agent {}", agent_name)
        return False
    return True
