"""Parse raw Claude session JSONL files into common transcript events.

Reimplements the conversion logic from mngr_claude's common_transcript.sh
in pure Python. Handles user messages, assistant messages with tool calls,
and tool result events.
"""

from __future__ import annotations

import json
import re
from typing import Any

from imbue.system_interface.claude_auth_patterns import is_auth_error_text

_MAX_INPUT_PREVIEW_LENGTH = 200
_MAX_OUTPUT_LENGTH = 2000

_SOURCE = "claude/common_transcript"

_AGENT_ID_PATTERN = re.compile(r"agentId:\s*(\S+)")

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
        except json.JSONDecodeError:
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
            if len(input_preview) > _MAX_INPUT_PREVIEW_LENGTH:
                input_preview = input_preview[:_MAX_INPUT_PREVIEW_LENGTH] + "..."

            if call_id and tool_name:
                tool_name_by_call_id[call_id] = tool_name

            tool_calls.append(
                {
                    "tool_call_id": call_id,
                    "tool_name": tool_name,
                    "input_preview": input_preview,
                }
            )

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
            text = _extract_text_content(content)
            # Drop Claude Code's resume bookkeeping -- its own UI hides it, so do we.
            if text and not _is_resume_continuation_marker(raw):
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

            if len(result_content) > _MAX_OUTPUT_LENGTH:
                result_content = result_content[:_MAX_OUTPUT_LENGTH] + "..."

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
