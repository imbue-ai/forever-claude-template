"""Parse raw Claude session JSONL files into common transcript events.

Reimplements the conversion logic from mngr_claude's common_transcript.sh
in pure Python. Handles user messages, assistant messages with tool calls,
and tool result events.
"""

from __future__ import annotations

import json
import re
from typing import Any

from loguru import logger as _loguru_logger
from tk_command_parsing.parser import parse_command

from imbue.system_interface.claude_auth_patterns import is_auth_error_text

logger = _loguru_logger

_MAX_INPUT_PREVIEW_LENGTH = 200
_MAX_OUTPUT_LENGTH = 2000

# tk lifecycle commands print machine-readable decoration on stdout that the
# chat progress view reads back from the transcript: `Updated <id> -> <status>`
# on every transition (positions a step's open/close) and, for steps,
# `tk-step <id> title:`/`summary:` lines (carry the title and close summary).
# The format is defined in `vendor/tk/ticket` (cmd_create/start/close) and also
# parsed by the frontend (`turn-grouping.ts`); keep all three in sync.
# These must survive output truncation (e.g. when a tk command is batched after
# a verbose one whose output pushes the line past the limit), so a step's
# structure and decoration are never lost to truncation -- see
# `_truncate_tool_output`.
_TK_OUTPUT_DECORATION_PATTERN = re.compile(
    r"Updated \S+ -> (?:open|in_progress|closed)|tk-step \S+ (?:title|summary): .*"
)

# The tk subcommands whose Bash calls carry the step titles/summaries that the
# chat progress view's historical input-preview fallback reads -- a command
# invoking one of these is exempted from the 200-char `input_preview` truncation
# below so batched `tk create --step` forms and long `tk close <id> "<summary>"`
# calls survive intact. Recognition is delegated to the shared
# `tk_command_parsing` parser (see `_is_tk_lifecycle_call`).
_TK_LIFECYCLE_VERBS = frozenset({"create", "start", "close"})

_SOURCE = "claude/common_transcript"

_AGENT_ID_PATTERN = re.compile(r"agentId:\s*(\S+)")

# Sentinel text Claude writes to the user channel when the user interrupts a
# turn (e.g. presses Esc mid-tool-use). It is a control marker, not real user
# input -- emitting it as a ``user_message`` event would pin the activity
# indicator on "Thinking..." after every interrupt, since the transcript-tail
# heuristic would treat it as "user just spoke, Claude hasn't replied yet."
_INTERRUPT_SENTINEL_TEXT = "[Request interrupted by user]"

# Claude Code's resume bookkeeping. Whenever ``claude --resume`` reloads a
# session whose previous turn did not finish cleanly (the turn was interrupted,
# or the process was stopped or crashed mid-turn), the framework injects a
# synthetic turn-pair to close the dangling turn: an ``isMeta`` user message
# with exactly this text, answered by a synthetic-model assistant message (see
# ``_SYNTHETIC_MODEL``). This pair is inert -- Claude Code's own UI hides both,
# and the agent never acts on it -- so the chat transcript view hides it too;
# otherwise the pair would surface as a spurious exchange the user never had.
_RESUME_CONTINUATION_TEXT = "Continue from where you left off."

# Model value Claude Code stamps on assistant messages the framework generates
# itself, as opposed to real model output. Note this model is NOT unique to the
# resume turn-pair's reply: Claude Code also stamps it on API-error and auth
# (e.g. "API Error: 529 Overloaded", "Please run /login") notices, which the
# user does need to see. So the synthetic model alone is not enough to hide a
# message -- the text must also match (see ``_is_resume_no_response_reply``).
_SYNTHETIC_MODEL = "<synthetic>"

# Exact text of the synthetic assistant message that answers the resume
# continuation marker. The resume turn-pair is "Continue from where you left
# off." -> "No response requested."; this is the reply half.
_NO_RESPONSE_REQUESTED_TEXT = "No response requested."

# Claude Code records a message the user typed while the agent was busy (a
# "queued" message) not as a normal ``user`` line but as an ``attachment`` event
# of this type. Its ``commandMode`` distinguishes the verbatim user prompt
# (``prompt``) from background-task completion notices (``task-notification``),
# which are framework-generated and not user turns. Without parsing the
# ``prompt`` form, a queued user message yields no ``user_message`` event at all:
# it never appears as a user bubble, and the frontend's optimistic "Queued"
# bubble never reconciles -- so it stays up even after the agent has received and
# answered the message. (Empirically a queued message is recorded EITHER as this
# attachment OR, on older Claude Code versions, as a plain ``user`` line, never
# both, so parsing it here does not double-render.)
_QUEUED_COMMAND_ATTACHMENT_TYPE = "queued_command"
_QUEUED_COMMAND_PROMPT_MODE = "prompt"

# A slash command the user types (``/foo bar``) is not recorded verbatim: Claude
# Code expands it into an XML-ish block carrying the command name, a display
# message, and the trailing arguments, e.g.
#     <command-message>foo</command-message>
#     <command-name>/foo</command-name>
#     <command-args>bar</command-args>
# The three tags appear in varying order (built-ins lead with <command-name>,
# custom commands with <command-message>), so they are matched individually
# rather than positionally. We rebuild the original ``/foo bar`` text so (a) the
# rendered user bubble shows what the user actually typed instead of the raw
# expansion and (b) the frontend's optimistic-message reconciliation -- which
# matches a pending bubble to its transcript event by whitespace-normalized
# content -- finds the match (otherwise the bubble is stranded; see
# PendingMessages.ts).
_COMMAND_NAME_PATTERN = re.compile(r"<command-name>(.*?)</command-name>", re.DOTALL)
_COMMAND_ARGS_PATTERN = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)


def _normalize_slash_command(text: str) -> str:
    """Rebuild ``/name args`` from a Claude Code slash-command expansion.

    Returns ``text`` unchanged when it is not a command expansion (no
    ``<command-name>`` tag, or an empty command name).
    """
    name_match = _COMMAND_NAME_PATTERN.search(text)
    if name_match is None:
        return text
    command = name_match.group(1).strip()
    if not command:
        return text
    args_match = _COMMAND_ARGS_PATTERN.search(text)
    args = args_match.group(1).strip() if args_match is not None else ""
    return f"{command} {args}".strip()


def _extract_text_content(content: str | list[dict[str, Any]] | Any) -> str:
    """Extract plain text from a message content field (string or list of blocks)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            if text:
                parts.append(text)
    return "\n".join(parts)


def _has_tool_results_only(content: str | list[Any] | Any) -> bool:
    """Check if a content list contains only tool_result blocks (no user text)."""
    if isinstance(content, str):
        return False
    if not isinstance(content, list):
        return True
    for block in content:
        if isinstance(block, dict):
            block_type = block.get("type", "")
            if block_type not in ("tool_result",):
                return False
        elif isinstance(block, str):
            return False
    return True


def _extract_subagent_id(structured_agent_id: str | None, result_content: str) -> str | None:
    """Resolve the subagent id for an Agent tool_result.

    Prefers the structured toolUseResult.agentId field, falling back to the
    `agentId: <id>` text trailer in the tool result content. Newer Claude Code
    versions may emit only the structured field; older versions or nested
    subagents may emit only the trailer.
    """
    if structured_agent_id:
        return structured_agent_id
    if not result_content:
        return None
    agent_id_match = _AGENT_ID_PATTERN.search(result_content)
    if agent_id_match:
        return agent_id_match.group(1)
    return None


def _make_event_id(uuid: str, suffix: str) -> str:
    """Derive a deterministic event_id from the source UUID and a suffix."""
    return f"{uuid}-{suffix}"


def _is_tk_lifecycle_call(tool_name: str, tool_input: Any) -> bool:
    """True for a Bash call whose command invokes a tk/ticket create|start|close.
    Their `input_preview` is exempted from truncation so batched multi-create
    commands and long close summaries survive intact for the chat progress
    view's historical input-preview fallback.

    Recognition uses the shared `tk_command_parsing` shlex parser rather than a
    regex, so a `tk close ...` merely *mentioned* inside another command's quoted
    argument (e.g. `echo "remember to tk close s1"`) is not mistaken for a real
    tk lifecycle call -- the same shell-awareness the standalone-command gate
    hook relies on."""
    if tool_name != "Bash" or not isinstance(tool_input, dict):
        return False
    command = tool_input.get("command", "")
    if not isinstance(command, str):
        return False
    parsed = parse_command(command)
    if parsed is None:
        return False
    return any(segment.tk_verb in _TK_LIFECYCLE_VERBS for segment in parsed.segments)


def _truncate_tool_output(content: str) -> str:
    """Truncate a tool result to the head limit, but keep any tk decoration
    lines (`Updated <id> -> <status>` and `tk-step <id> title|summary: ...`)
    that fall past the cut, appended after the truncation marker. This preserves
    the progress view's step structure and decoration even when a tk command's
    output is pushed past the limit."""
    if len(content) <= _MAX_OUTPUT_LENGTH:
        return content
    head = content[:_MAX_OUTPUT_LENGTH]
    preserved = [m.group(0) for m in _TK_OUTPUT_DECORATION_PATTERN.finditer(content) if m.end() > _MAX_OUTPUT_LENGTH]
    if preserved:
        return head + "...\n" + "\n".join(preserved)
    return head + "..."


def _is_resume_continuation_marker(raw: dict[str, Any]) -> bool:
    """True if ``raw`` is Claude Code's synthetic resume-continuation user message.

    The marker is an ``isMeta`` user message whose text is exactly the
    resume-continuation sentinel (see ``_RESUME_CONTINUATION_TEXT``). Gating on
    ``isMeta`` ensures a human who happens to type the same words is still
    rendered. This is bookkeeping the chat transcript view must hide.
    """
    if not raw.get("isMeta"):
        return False
    text = _extract_text_content(raw.get("message", {}).get("content"))
    return text.strip() == _RESUME_CONTINUATION_TEXT


def _is_resume_no_response_reply(message: dict[str, Any]) -> bool:
    """True if ``message`` is the synthetic reply half of the resume turn-pair.

    The reply is an assistant message that is BOTH stamped with the synthetic
    model AND has exactly the no-response text. Both conditions are required:
    the synthetic model alone also covers API-error and auth notices the user
    must see, and the text alone could be a real agent turn that happens to say
    those words. Only their conjunction is the inert bookkeeping reply, which
    the chat transcript view hides to match Claude Code's own UI.
    """
    if message.get("model") != _SYNTHETIC_MODEL:
        return False
    return _extract_text_content(message.get("content")).strip() == _NO_RESPONSE_REQUESTED_TEXT


def parse_session_lines(
    lines: list[str],
    existing_event_ids: set[str] | None = None,
    tool_name_by_call_id: dict[str, str] | None = None,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Parse raw Claude session JSONL lines into common transcript events.

    Args:
        lines: Raw JSONL lines from a Claude session file.
        existing_event_ids: Set of event IDs already emitted, for deduplication.
            If None, no deduplication is performed.
        tool_name_by_call_id: Mutable mapping from tool_use_id to tool_name,
            carried across calls for cross-message tool name resolution.
            If None, a fresh dict is used.
        session_id: Identifier for the session file these lines came from.
            If provided, each event will include a "session_id" field.

    Returns:
        List of common transcript event dicts, sorted by timestamp.
    """
    if existing_event_ids is None:
        existing_event_ids = set()
    if tool_name_by_call_id is None:
        tool_name_by_call_id = {}

    new_events: list[tuple[str, dict[str, Any]]] = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as e:
            logger.debug("Skipping malformed JSONL line: {}", e)
            continue

        event_type: str = raw.get("type", "")
        uuid: str = raw.get("uuid", "")
        timestamp: str = raw.get("timestamp", "")

        if not uuid or not timestamp:
            continue

        if event_type == "assistant":
            _parse_assistant_message(
                raw, uuid, timestamp, existing_event_ids, tool_name_by_call_id, new_events, session_id
            )
        elif event_type == "user":
            _parse_user_message(raw, uuid, timestamp, existing_event_ids, tool_name_by_call_id, new_events, session_id)
        elif event_type == "attachment":
            _parse_queued_command_attachment(raw, uuid, timestamp, existing_event_ids, new_events, session_id)
        # Skip: progress, file-history-snapshot, system, result, etc.

    new_events.sort(key=lambda x: x[0])
    return [event for _, event in new_events]


def _parse_assistant_message(
    raw: dict[str, Any],
    uuid: str,
    timestamp: str,
    existing_event_ids: set[str],
    tool_name_by_call_id: dict[str, str],
    new_events: list[tuple[str, dict[str, Any]]],
    session_id: str | None = None,
) -> None:
    event_id = _make_event_id(uuid, "assistant")
    if event_id in existing_event_ids:
        return

    message: dict[str, Any] = raw.get("message", {})

    # Drop Claude Code's resume bookkeeping -- its own UI hides it, so do we.
    if _is_resume_no_response_reply(message):
        return

    content_blocks: list[Any] = message.get("content", [])
    model: str = message.get("model", "unknown")
    stop_reason: str | None = message.get("stop_reason")
    usage_raw: dict[str, Any] = message.get("usage", {})

    text_parts: list[str] = []
    tool_calls: list[dict[str, str]] = []
    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type", "")
        if block_type == "text":
            text = block.get("text", "")
            if text:
                text_parts.append(text)
        elif block_type == "tool_use":
            call_id: str = block.get("id", "")
            tool_name: str = block.get("name", "")
            tool_input = block.get("input", {})
            input_preview = json.dumps(tool_input, separators=(",", ":"))
            if len(input_preview) > _MAX_INPUT_PREVIEW_LENGTH and not _is_tk_lifecycle_call(tool_name, tool_input):
                input_preview = input_preview[:_MAX_INPUT_PREVIEW_LENGTH] + "..."

            if call_id and tool_name:
                tool_name_by_call_id[call_id] = tool_name

            tool_call: dict[str, str] = {
                "tool_call_id": call_id,
                "tool_name": tool_name,
                "input_preview": input_preview,
            }
            # For Agent tool calls, surface the description and subagent_type from the
            # tool input directly. These let the frontend render the rich subagent card
            # (label + agent-type badge) the instant the call appears, before the subagent
            # is linked to its session.
            if tool_name == "Agent" and isinstance(tool_input, dict):
                description = tool_input.get("description")
                subagent_type = tool_input.get("subagent_type")
                if isinstance(description, str) and description:
                    tool_call["description"] = description
                if isinstance(subagent_type, str) and subagent_type:
                    tool_call["subagent_type"] = subagent_type

            tool_calls.append(tool_call)

    usage: dict[str, Any] | None = None
    if usage_raw:
        usage = {
            "input_tokens": usage_raw.get("input_tokens", 0),
            "output_tokens": usage_raw.get("output_tokens", 0),
            "cache_read_tokens": usage_raw.get("cache_read_input_tokens"),
            "cache_write_tokens": usage_raw.get("cache_creation_input_tokens"),
        }

    joined_text = "\n".join(text_parts)
    event: dict[str, Any] = {
        "timestamp": timestamp,
        "type": "assistant_message",
        "event_id": event_id,
        "source": _SOURCE,
        "role": "assistant",
        "model": model,
        "text": joined_text,
        "tool_calls": tool_calls,
        "stop_reason": stop_reason,
        "usage": usage,
        "message_uuid": uuid,
        "is_auth_error": is_auth_error_text(joined_text),
    }
    if session_id is not None:
        event["session_id"] = session_id
    existing_event_ids.add(event_id)
    new_events.append((timestamp, event))


def _parse_user_message(
    raw: dict[str, Any],
    uuid: str,
    timestamp: str,
    existing_event_ids: set[str],
    tool_name_by_call_id: dict[str, str],
    new_events: list[tuple[str, dict[str, Any]]],
    session_id: str | None = None,
) -> None:
    message: dict[str, Any] = raw.get("message", {})
    content = message.get("content")

    tool_use_result = raw.get("toolUseResult")
    structured_agent_id: str | None = None
    if isinstance(tool_use_result, dict):
        agent_id_value = tool_use_result.get("agentId")
        if isinstance(agent_id_value, str) and agent_id_value:
            structured_agent_id = agent_id_value

    # Emit user text message if there is actual user text
    if not _has_tool_results_only(content):
        event_id = _make_event_id(uuid, "user")
        if event_id not in existing_event_ids:
            text = _normalize_slash_command(_extract_text_content(content))
            if text and text.strip() != _INTERRUPT_SENTINEL_TEXT and not _is_resume_continuation_marker(raw):
                event: dict[str, Any] = {
                    "timestamp": timestamp,
                    "type": "user_message",
                    "event_id": event_id,
                    "source": _SOURCE,
                    "role": "user",
                    "content": text,
                    "message_uuid": uuid,
                }
                if session_id is not None:
                    event["session_id"] = session_id
                existing_event_ids.add(event_id)
                new_events.append((timestamp, event))

    # Emit tool result events for any tool_result blocks
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_result":
                continue
            tool_call_id: str = block.get("tool_use_id", "")
            if not tool_call_id:
                continue

            event_id = _make_event_id(uuid, f"tool_result-{tool_call_id}")
            if event_id in existing_event_ids:
                continue

            # Extract output text
            result_content = block.get("content", "")
            if isinstance(result_content, list):
                parts: list[str] = []
                for item in result_content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        parts.append(item.get("text", ""))
                    elif isinstance(item, str):
                        parts.append(item)
                result_content = "\n".join(parts)
            elif not isinstance(result_content, str):
                result_content = str(result_content)

            tool_name = tool_name_by_call_id.get(tool_call_id, "unknown")

            # Extract subagent ID BEFORE truncation (the trailer may be at the end).
            extracted_subagent_id: str | None = None
            if tool_name == "Agent":
                extracted_subagent_id = _extract_subagent_id(structured_agent_id, result_content)

            result_content = _truncate_tool_output(result_content)

            event = {
                "timestamp": timestamp,
                "type": "tool_result",
                "event_id": event_id,
                "source": _SOURCE,
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "output": result_content,
                "is_error": bool(block.get("is_error", False)),
                "message_uuid": uuid,
            }
            if session_id is not None:
                event["session_id"] = session_id

            if extracted_subagent_id:
                event["subagent_id"] = extracted_subagent_id

            existing_event_ids.add(event_id)
            new_events.append((timestamp, event))


def _parse_queued_command_attachment(
    raw: dict[str, Any],
    uuid: str,
    timestamp: str,
    existing_event_ids: set[str],
    new_events: list[tuple[str, dict[str, Any]]],
    session_id: str | None = None,
) -> None:
    """Emit a ``user_message`` event for a message the user queued while busy.

    Claude Code writes such a message as a ``queued_command`` attachment (see
    ``_QUEUED_COMMAND_ATTACHMENT_TYPE``) rather than a normal ``user`` line.
    Only the ``prompt`` command mode carries verbatim user text; the
    ``task-notification`` mode is a framework-generated background-task notice
    and is left unparsed (it is not a user turn).
    """
    attachment = raw.get("attachment")
    if not isinstance(attachment, dict):
        return
    if attachment.get("type") != _QUEUED_COMMAND_ATTACHMENT_TYPE:
        return
    if attachment.get("commandMode") != _QUEUED_COMMAND_PROMPT_MODE:
        return
    prompt = attachment.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return
    # A queued message can itself be a slash command; normalize it the same way
    # a non-queued one is handled in ``_parse_user_message`` so it renders as the
    # typed text and reconciles against its optimistic bubble.
    prompt = _normalize_slash_command(prompt)

    event_id = _make_event_id(uuid, "queued")
    if event_id in existing_event_ids:
        return

    event: dict[str, Any] = {
        "timestamp": timestamp,
        "type": "user_message",
        "event_id": event_id,
        "source": _SOURCE,
        "role": "user",
        "content": prompt,
        "message_uuid": uuid,
    }
    if session_id is not None:
        event["session_id"] = session_id
    existing_event_ids.add(event_id)
    new_events.append((timestamp, event))
