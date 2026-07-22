"""Unit tests for eval_decider's pure helpers (no network)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))  # so eval_decider's `import eval_wait_watcher` resolves

_spec = importlib.util.spec_from_file_location("eval_decider", _SCRIPTS / "eval_decider.py")
assert _spec is not None and _spec.loader is not None
eval_decider = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(eval_decider)


def test_render_conversation_keeps_user_facing_turns() -> None:
    events = [
        {"type": "user_message", "content": "hi what can you do"},
        {"type": "assistant_message", "text": ""},  # internal placeholder, dropped
        {"type": "assistant_message", "text": "I can help with three things."},
        {"type": "user_message", "content": ""},  # empty, dropped
    ]
    assert eval_decider._render_conversation(events) == (
        "YOU (client): hi what can you do\n\nAGENT: I can help with three things."
    )


def test_prompt_includes_persona_and_conversation() -> None:
    prompt = eval_decider._prompt("A busy non-technical founder.", "YOU (client): hi\n\nAGENT: hello")
    assert "A busy non-technical founder." in prompt
    assert "YOU (client): hi" in prompt and "AGENT: hello" in prompt


def test_prompt_without_persona_omits_persona_line() -> None:
    prompt = eval_decider._prompt("", "AGENT: hello")
    assert "persona" not in prompt.lower()
    assert "AGENT: hello" in prompt


def test_decide_falls_back_without_key(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert eval_decider.decide_next_message("agent-1", "persona") == eval_decider._FALLBACK
