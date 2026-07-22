"""Parse a codex agent's raw rollout JSONL into the web-UI event schema.

Codex writes its conversation as a "rollout" -- append-only JSONL where each line
is ``{"timestamp", "type", "payload": {"type", ...}}``. mngr_codex mirrors the live
rollout verbatim (no reschematising) to a stable per-agent path
``<agent_state_dir>/logs/codex_transcript/events.jsonl`` (its ``stream_transcript.sh``),
which is what :class:`CodexSessionWatcher` tails.

This module maps those raw rollout lines into the *exact* dict shape the web UI
consumes -- the same shape ``claude_session_parser`` emits for claude -- so the
transport (SSE), the frontend, and the activity tracker need no codex-specific
branches. It is the codex analogue of ``claude_session_parser``.

Sourcing rule (confirmed against codex ``policy.rs`` + real rollouts, see
blueprint/codex-rich-transcript): ``response_item`` lines are the canonical
conversation state; ``event_msg`` lines are a derived live-display stream. We build
the body from ``response_item`` -- **except** two things taken from ``event_msg``:
(1) user bubbles, from ``user_message`` (the clean human-typed prompt); and (2) the
``turn_aborted`` marker (a user interrupt), used to clear a stuck activity dot.
A hosted web search comes from its ``response_item`` ``web_search_call`` (not the
transient begin/end event_msgs). ``response_item`` role=user is the *model-facing*
user role: the human prompt PLUS
injected ``AGENTS.md`` / ``<environment_context>`` / ``<turn_aborted>`` /
``<subagent_notification>`` content, which we do not want as chat bubbles. Everything
else in ``event_msg`` (``agent_message`` display echoes, ``token_count``, ``task_*``)
is skipped in this core cut.

Lossy by design for this first cut -- all deferred to later slices: ``usage``
(``token_count`` -> Phase 2, and coarse), ``is_auth_error`` (lives in codex's
``logs_2.sqlite``, never the transcript), subagent linkage, tk step-progress.
``stop_reason`` is left null.

Event ids prefer codex's own stable identity (the assistant message ``id``, or a
tool call's ``call_id``) so the watcher dedups codex 0.144.3's re-serialised
duplicates (the same message written to the rollout more than once by the
"paginated" / world_state persistence). Where codex gives no id (an ``event_msg``
``user_message``), we synthesise one from its timestamp + text (see
``_stable_user_event_id``) rather than the physical line index -- position-independent,
so if a rollout is compressed and re-materialised (repointing the marker and forcing a
re-read from byte 0) the same user bubble dedups instead of duplicating.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# Kept as ``codex/common_transcript`` to match the ``<harness>/common_transcript``
# label ``claude_session_parser`` stamps -- "common" here means the normalized/common
# event *form*, not the on-disk common-transcript file (which we do NOT read).
# Nothing in the pipeline branches on this string.
_SOURCE = "codex/common_transcript"

# Codex rollout messages never carry a per-message model slug, so surface the same
# placeholder ``claude_session_parser`` uses when the model is absent, keeping the
# frontend's non-optional ``model`` field populated.
_UNKNOWN_MODEL = "unknown"

_MAX_INPUT_PREVIEW_LENGTH = 200
_MAX_OUTPUT_LENGTH = 2000


def _join_content_text(content: Any, want_type: str) -> str:
    """Join the ``text`` of ``content`` blocks whose ``type`` is ``want_type``."""
    if not isinstance(content, list):
        return ""
    return "".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == want_type and block.get("text")
    )


def _stringify_output(output: Any) -> str:
    """A ``*_output.output`` is either a string or a list of content items; flatten
    to a truncated string."""
    if isinstance(output, str):
        text = output
    elif isinstance(output, list):
        parts: list[str] = []
        for item in output:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("output") or ""))
            elif isinstance(item, str):
                parts.append(item)
            else:
                # other item shapes carry no text
                continue
        text = "".join(parts)
    else:
        text = "" if output is None else str(output)
    if len(text) > _MAX_OUTPUT_LENGTH:
        return text[:_MAX_OUTPUT_LENGTH] + "..."
    return text


def _tool_call_input_preview(payload: dict[str, Any]) -> str:
    """``function_call`` carries ``arguments`` (a JSON string); ``custom_tool_call``
    carries ``input`` (raw text, e.g. an apply_patch body)."""
    raw = payload.get("arguments")
    if raw is None:
        raw = payload.get("input")
    text = "" if raw is None else str(raw)
    if len(text) > _MAX_INPUT_PREVIEW_LENGTH:
        return text[:_MAX_INPUT_PREVIEW_LENGTH] + "..."
    return text


def _assistant_event(timestamp: str, event_id: str, *, text: str, tool_calls: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "type": "assistant_message",
        "event_id": event_id,
        "source": _SOURCE,
        "role": "assistant",
        "model": _UNKNOWN_MODEL,
        "text": text,
        "tool_calls": tool_calls,
        # deferred (derive from task_complete later)
        "stop_reason": None,
        # deferred (token_count -> Phase 2)
        "usage": None,
        "message_uuid": event_id,
        # deferred (codex auth errors live in logs_2.sqlite)
        "is_auth_error": False,
    }


def _stable_user_event_id(timestamp: str, content: str) -> str:
    """A content-derived, position-independent event id for a user bubble.

    An ``event_msg`` ``user_message`` carries no codex id, so we synthesise one from
    its ``timestamp`` + text. Unlike the physical line index, this is stable across a
    re-read of the *same* message from a rotated/materialised rollout (codex compresses
    finished rollouts to ``.zst`` then re-expands them, which can repoint the marker and
    force the watcher to re-read from byte 0 -- a line-index id would change and the
    bubble would duplicate). Two genuinely distinct sends never collide: identical text
    at the same millisecond timestamp is not something a human can produce.
    """
    digest = hashlib.sha1(f"{timestamp}\x00{content}".encode("utf-8", "replace")).hexdigest()[:16]
    return f"codex-user-{digest}"


def _web_search_query(action: Any) -> str:
    """Pull the query from a ``web_search_call``'s ``action`` (Search variant:
    ``query``, or the first of ``queries``). Empty string when absent."""
    if not isinstance(action, dict):
        return ""
    query = action.get("query")
    if isinstance(query, str) and query:
        return query
    queries = action.get("queries")
    if isinstance(queries, list):
        for candidate in queries:
            if isinstance(candidate, str) and candidate:
                return candidate
    return ""


def parse_codex_rollout_line(
    record: dict[str, Any],
    line_index: int,
    tool_name_by_call_id: dict[str, str],
) -> list[dict[str, Any]]:
    """Map one codex rollout line to zero or more UI event dicts (``[]`` to skip).

    Returns a *list* because one rollout line can expand to more than one event: a
    completed hosted web search is a single self-contained ``web_search_call`` item,
    which we emit as a tool_use + its matching tool_result so it renders as a done
    "Searching the web ..." bubble without leaving an unmatched (stuck) call.

    ``line_index`` is the stable physical line number (for event-id synthesis).
    ``tool_name_by_call_id`` is a mutable cross-line map so a ``function_call_output``
    can recover its tool name from the earlier ``function_call``.
    """
    outer = record.get("type")
    payload = record.get("payload")
    timestamp = record.get("timestamp", "")
    if not isinstance(payload, dict) or not isinstance(timestamp, str):
        return []
    payload_type = payload.get("type")

    # --- event_msg: the clean human prompt + the turn-abort marker ---
    if outer == "event_msg":
        if payload_type == "user_message":
            text = payload.get("message")
            if isinstance(text, str) and text:
                event_id = _stable_user_event_id(timestamp, text)
                return [
                    {
                        "timestamp": timestamp,
                        "type": "user_message",
                        "event_id": event_id,
                        "source": _SOURCE,
                        "role": "user",
                        "content": text,
                        "message_uuid": event_id,
                    }
                ]
            return []
        # A user interrupt aborts the turn. Codex does NOT persist the synthetic
        # aborted tool output, so an in-flight tool call would otherwise stay
        # unmatched forever and pin the activity dot at "Running". Emit a lightweight
        # turn_aborted marker; the activity layer treats it as resolving every
        # still-open tool call (see ``activity_state.pending_tool_call``).
        if payload_type == "turn_aborted":
            event_id = f"codex-{line_index}-turn_aborted"
            return [
                {
                    "timestamp": timestamp,
                    "type": "turn_aborted",
                    "event_id": event_id,
                    "source": _SOURCE,
                    "message_uuid": event_id,
                }
            ]
        return []

    if outer != "response_item":
        # session_meta, turn_context -> drop
        return []

    # --- response_item: assistant messages + tool calls/results + hosted web search ---
    if payload_type == "message":
        if payload.get("role") == "assistant":
            # codex re-serialises history; each copy shares the message ``id``, so
            # keying the event id on it dedups the copies (fall back to line index).
            msg_id = payload.get("id")
            event_id = f"codex-{msg_id}" if isinstance(msg_id, str) and msg_id else f"codex-{line_index}-assistant"
            return [
                _assistant_event(
                    timestamp,
                    event_id,
                    text=_join_content_text(payload.get("content"), "output_text"),
                    tool_calls=[],
                )
            ]
        # role=user (and developer/system) -> skip; user bubbles come from event_msg.
        return []

    if payload_type in ("function_call", "custom_tool_call"):
        call_id = str(payload.get("call_id", ""))
        tool_name = str(payload.get("name", ""))
        if call_id and tool_name:
            tool_name_by_call_id[call_id] = tool_name
        # Same dedup rationale: a re-serialised tool call keeps its ``call_id``.
        event_id = f"codex-call-{call_id}" if call_id else f"codex-{line_index}-assistant"
        return [
            _assistant_event(
                timestamp,
                event_id,
                text="",
                tool_calls=[
                    {
                        "tool_call_id": call_id,
                        "tool_name": tool_name,
                        "input_preview": _tool_call_input_preview(payload),
                    }
                ],
            )
        ]

    if payload_type in ("function_call_output", "custom_tool_call_output"):
        call_id = str(payload.get("call_id", ""))
        event_id = f"codex-result-{call_id}" if call_id else f"codex-{line_index}-tool_result"
        return [
            {
                "timestamp": timestamp,
                "type": "tool_result",
                "event_id": event_id,
                "source": _SOURCE,
                "tool_call_id": call_id,
                "tool_name": tool_name_by_call_id.get(call_id, ""),
                "output": _stringify_output(payload.get("output")),
                "is_error": False,
                "message_uuid": event_id,
            }
        ]

    # A hosted web search is persisted (all modes) as a single, self-contained
    # ``web_search_call`` response_item -- keyed on ``id`` (there is no call_id), with
    # the query under ``action`` -- NOT as the transient ``web_search_begin``/``_end``
    # event_msgs (begin is never persisted; end is legacy-only). It is already done
    # when written, so we synthesise BOTH the tool call and its matching result from
    # this one line: a "Searching the web <query>" bubble that renders complete,
    # never an unmatched call that would stick the dot. (No in-flight window to
    # caption live -- codex hands us the search already finished.)
    if payload_type == "web_search_call":
        item_id = str(payload.get("id", "")) or f"line{line_index}"
        query = _web_search_query(payload.get("action"))
        input_preview = json.dumps({"query": query}, separators=(",", ":")) if query else ""
        call_event_id = f"codex-call-{item_id}"
        result_event_id = f"codex-result-{item_id}"
        return [
            _assistant_event(
                timestamp,
                call_event_id,
                text="",
                tool_calls=[{"tool_call_id": item_id, "tool_name": "web_search", "input_preview": input_preview}],
            ),
            {
                "timestamp": timestamp,
                "type": "tool_result",
                "event_id": result_event_id,
                "source": _SOURCE,
                "tool_call_id": item_id,
                "tool_name": "web_search",
                "output": query,
                "is_error": False,
                "message_uuid": result_event_id,
            },
        ]

    return []
