"""Role-play the eval client for a DECIDE_FROM_PERSONA turn.

Given the conversation so far (from the local system_interface) and the case persona, ask the
Anthropic API for the single next casual thing the client would say -- one short sentence or a few
words -- and return it. Pure stdlib (urllib); the key is slotted into scripts/test_case_metadata.json
by the harness (config["anthropic_api_key"]).
"""

from __future__ import annotations

import json
import urllib.request

import eval_wait_watcher as watcher

_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_MODEL = "claude-opus-4-8"
_MAX_TOKENS = 64
_FALLBACK = "Sounds good."


def _render_conversation(events: list[dict]) -> str:
    """The user-facing conversation so far: user_message.content / non-empty assistant_message.text."""
    lines = []
    for event in events:
        if event.get("type") == "assistant_message":
            text = (event.get("text") or "").strip()
            if text:
                lines.append("AGENT: {}".format(text))
        elif event.get("type") == "user_message":
            content = (event.get("content") or "").strip()
            if content:
                lines.append("YOU (client): {}".format(content))
    return "\n\n".join(lines)


def _prompt(persona: str, conversation: str) -> str:
    who = "You are the client in this conversation."
    if persona:
        who += " Your persona: {}".format(persona)
    return (
        "{who} An AI agent (AGENT) is building software for you. Below is the conversation so far.\n\n"
        "Reply with the single next thing you would casually say to keep it moving -- ONE short "
        "sentence or just a few words, in a natural, non-technical voice. Output only that message, "
        "nothing else.\n\nConversation so far:\n{conversation}"
    ).format(who=who, conversation=conversation)


def decide_next_message(agent_id: str, persona: str, api_key: str) -> str:
    """Ask the API for the client's next casual line. Falls back to 'Sounds good.' on any error, so a
    flaky API call never stalls the eval."""
    if not api_key:
        print("[eval] no anthropic_api_key in config -- sending '{}'".format(_FALLBACK), flush=True)
        return _FALLBACK
    try:
        conversation = _render_conversation(watcher.fetch_all_events(agent_id))
        body = json.dumps({
            "model": _MODEL, "max_tokens": _MAX_TOKENS,
            "messages": [{"role": "user", "content": _prompt(persona, conversation)}],
        }).encode("utf-8")
        request = urllib.request.Request(
            _ANTHROPIC_URL, data=body, method="POST",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            data = json.loads(response.read().decode("utf-8"))
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
        return text or _FALLBACK
    except Exception as exc:  # a role-play API hiccup must not stall the run
        print("[eval] decide_from_persona failed ({}) -- sending '{}'".format(exc, _FALLBACK), flush=True)
        return _FALLBACK
