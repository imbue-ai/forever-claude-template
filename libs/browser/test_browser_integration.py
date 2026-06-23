"""Integration tests for the browser session layer.

Two kinds:
- A real headless-Chromium test of the steel-style path (spawn -> CDP screencast
  frames -> input dispatch -> open a 2nd tab -> active-tab follow). It skips when
  Chromium isn't installed (CI runners without the deferred-install), so it never
  fails for lack of a browser; it runs on a host/compute that has Chromium.
- A browser-use-free test of the agent control state machine (submit -> agent
  control, queue a second prompt while busy, take-control -> stop + human),
  with Agent/ChatAnthropic mocked so it runs everywhere without an LLM or browser.
"""

import asyncio
from typing import Any

import pytest
from playwright.async_api import Error as PlaywrightError

from browser import session as bsession


class _FakeWS:
    def __init__(self) -> None:
        self.frames: list[str] = []
        self.events: list[dict[str, Any]] = []

    async def send_json(self, obj: dict[str, Any]) -> None:
        if obj.get("type") == "frame":
            self.frames.append(obj["data"])
        else:
            self.events.append(obj)


def test_live_browser_streams_and_accepts_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROWSER_HEADLESS", "1")

    async def go() -> None:
        manager = bsession.BrowserSessionManager()
        try:
            session = await manager.create()
        except (bsession.BrowserStartupError, PlaywrightError, OSError) as e:
            pytest.skip(f"Chromium unavailable in this environment: {e}")
        try:
            cast = _FakeWS()
            session.add_cast_socket(cast)
            await session.handle_cast_message({"type": "navigate", "url": "https://example.com"})
            for _ in range(20):
                await asyncio.sleep(0.5)
                if cast.frames:
                    break
            assert cast.frames, "expected at least one screencast frame"

            # Human input dispatch must not raise against the live target.
            await session.handle_cast_message(
                {"type": "mouse", "event": {"type": "mouseMoved", "x": 50, "y": 50, "button": "none"}}
            )

            # Open a second tab and confirm the view follows it (active switches).
            await session.handle_cast_message({"type": "tab", "action": "new", "url": "https://example.org"})
            await asyncio.sleep(2)
            tab_events = [e for e in cast.events if e.get("type") == "tabs"]
            assert tab_events, "expected a tab-list update after opening a tab"
            active = [t for t in tab_events[-1]["tabs"] if t["active"]]
            assert len(active) == 1 and "example.org" in active[0]["url"]
        finally:
            await manager.shutdown()

    asyncio.run(go())


class _FakeHistory:
    def model_thoughts(self) -> list[str]:
        return ["thinking about the task"]

    def model_actions(self) -> list[str]:
        return ["click(1)"]


class _FakeAgent:
    """Stand-in for browser_use.Agent: run() loops until stop()."""

    def __init__(self, **_kwargs: Any) -> None:
        self._stopped = False
        self.history = _FakeHistory()

    async def run(self, on_step_end: Any = None) -> _FakeHistory:
        if on_step_end is not None:
            await on_step_end(self)
        for _ in range(500):
            if self._stopped:
                break
            await asyncio.sleep(0.01)
        return self.history

    def stop(self) -> None:
        self._stopped = True


def test_agent_control_state_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(bsession, "Agent", _FakeAgent)
    monkeypatch.setattr(bsession, "ChatAnthropic", lambda **_kwargs: object())

    browser = bsession.LiveBrowser(session_id="t")
    browser._bu_session = object()  # run_agent only passes this through to the (mocked) Agent
    chat = _FakeWS()
    browser.add_chat_socket(chat)

    async def go() -> None:
        await browser.submit("do something")
        for _ in range(100):
            await asyncio.sleep(0.01)
            if browser.control_owner == "agent":
                break
        assert browser.control_owner == "agent"
        assert any(e.get("role") == "user" for e in chat.events)
        assert any(e.get("type") == "chat" and e.get("role") in ("thinking", "action") for e in chat.events)

        # A second prompt while the agent runs is queued, not started concurrently.
        await browser.submit("next thing")
        assert browser._queued_prompt == "next thing"
        assert any(e.get("type") == "queued" and e.get("text") == "next thing" for e in chat.events)

        # Take control: stop completely, drop the queue, hand control back (no resume).
        await browser.take_control()
        assert browser.control_owner == "human"
        assert browser._agent is None
        assert browser._queued_prompt is None

    asyncio.run(go())


def test_take_control_with_no_agent_is_safe() -> None:
    browser = bsession.LiveBrowser(session_id="t2")

    async def go() -> None:
        await browser.take_control()  # no agent running -> just confirms human control
        assert browser.control_owner == "human"
        await browser._stop_active_agent()  # no agent -> no-op

    asyncio.run(go())
