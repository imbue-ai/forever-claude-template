import asyncio
from pathlib import Path

import pytest

from browser import session as bsession


def test_parse_env_file_handles_quotes_and_comments() -> None:
    text = '# comment\nANTHROPIC_API_KEY=sk-ant-123\nQUOTED="a b c"\nEMPTY=\n'
    parsed = bsession._parse_env_file(text)
    assert parsed["ANTHROPIC_API_KEY"] == "sk-ant-123"
    assert parsed["QUOTED"] == "a b c"
    assert parsed["EMPTY"] == ""


def test_resolve_credentials_prefers_process_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-proc")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.example")
    api_key, base_url = bsession.resolve_anthropic_credentials()
    assert api_key == "sk-proc"
    assert base_url == "https://proxy.example"


def test_resolve_credentials_falls_back_to_host_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    (tmp_path / "env").write_text(
        "ANTHROPIC_API_KEY=sk-host\nANTHROPIC_BASE_URL=https://host.example\n"
    )
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    api_key, base_url = bsession.resolve_anthropic_credentials()
    assert api_key == "sk-host"
    assert base_url == "https://host.example"


def test_anthropic_key_status_reflects_availability(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    available, _ = bsession.anthropic_key_status()
    assert available is True
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("MNGR_HOST_DIR", raising=False)
    available, reason = bsession.anthropic_key_status()
    assert available is False
    assert "Anthropic API key" in reason


def test_deferred_install_ready_gates_on_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Only the Chromium marker gates now -- CDP streaming is headless, no Xvfb.
    play = tmp_path / "done.playwright"
    monkeypatch.setattr(bsession, "_PLAYWRIGHT_MARKER", play)
    ready, _ = bsession.deferred_install_ready()
    assert ready is False
    play.write_text("")
    ready, reason = bsession.deferred_install_ready()
    assert ready is True
    assert reason == "ready"


def test_control_state_toggles_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    browser = bsession.LiveBrowser(session_id="t")

    async def _exercise() -> None:
        await browser._set_control("agent")
        assert browser.control_owner == "agent"
        assert not browser._input_enabled.is_set()
        # While the agent drives, human input AND tab control are dropped (no raise).
        await browser.handle_cast_message({"type": "mouse", "event": {"type": "mouseMoved"}})
        await browser.handle_cast_message({"type": "tab", "action": "new"})
        await browser.handle_cast_message({"type": "navigate", "url": "https://example.com"})
        await browser._set_control("human")
        assert browser.control_owner == "human"
        assert browser._input_enabled.is_set()

    asyncio.run(_exercise())


def test_create_rejects_when_at_session_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    # The cap must reject before launching Chromium, so a small compute can't be OOM-ed.
    monkeypatch.setattr(bsession, "_MAX_SESSIONS", 2)
    mgr = bsession.BrowserSessionManager()
    mgr._sessions["a"] = object()  # type: ignore[assignment]
    mgr._sessions["b"] = object()  # type: ignore[assignment]

    async def _exercise() -> None:
        with pytest.raises(bsession.BrowserStartupError, match="Too many open browsers"):
            await mgr.create()

    asyncio.run(_exercise())


def test_submit_queues_while_agent_running() -> None:
    # With an agent already set, submit() must queue (not start a second run) and
    # surface a "queued" event; cancel_queue clears it.
    browser = bsession.LiveBrowser(session_id="q")
    browser._run_active = True  # pretend a run is in progress

    class _Chat:
        def __init__(self) -> None:
            self.events: list[dict] = []

        async def send_json(self, obj: dict) -> None:
            self.events.append(obj)

    chat = _Chat()
    browser.add_chat_socket(chat)  # type: ignore[arg-type]

    async def _exercise() -> None:
        await browser.submit("second task")
        assert browser._queued_prompt == "second task"
        assert any(e.get("type") == "queued" and e.get("text") == "second task" for e in chat.events)
        await browser.cancel_queue()
        assert browser._queued_prompt is None
        assert chat.events[-1] == {"type": "queued", "text": None}

    asyncio.run(_exercise())
