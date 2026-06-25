"""Integration tests for the browser fleet.

Three kinds:
- A real headless-Chromium test of the steel-style path (spawn -> CDP screencast
  frames -> input dispatch -> open a 2nd tab -> active-tab follow). It skips when
  Chromium isn't installed (CI runners without the deferred-install), so it never
  fails for lack of a browser; it runs on a host/compute that has Chromium.
- A browser-use-free test of the run-agent event stream + human take-control
  preemption, with Agent/ChatAnthropic mocked so it runs everywhere.
- HTTP-layer tests of the fleet endpoints (list / task stream / release / cap)
  via FastAPI's TestClient, with run_agent stubbed (no LLM, no browser).
"""

import asyncio
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from playwright.async_api import Error as PlaywrightError

from browser import manifest
from browser import runner
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


# Real Chromium: cold-start + screencast/nav exceeds the global 10s pytest timeout in CI
# (where Chromium is installed but the runner is slow), so give it room. If Chromium truly
# can't launch, the body's except still pytest.skip()s well within this.
@pytest.mark.timeout(120)
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
            session.add_cast_socket(cast)  # type: ignore[arg-type]
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
    def model_thoughts(self) -> list[Any]:
        return [{"next_goal": "do the thing", "thinking": "reasoning"}]

    def model_actions(self) -> list[Any]:
        return [{"click": {"index": 1}}]

    def final_result(self) -> str:
        return "all done"


class _FinishingAgent:
    """browser_use.Agent stand-in whose run() steps once and returns."""

    def __init__(self, **_kwargs: Any) -> None:
        self.history = _FakeHistory()

    async def run(self, on_step_end: Any = None, max_steps: int | None = None) -> _FakeHistory:
        if on_step_end is not None:
            await on_step_end(self)
        return self.history

    def stop(self) -> None:
        pass


class _BlockingAgent:
    """browser_use.Agent stand-in whose run() steps once then blocks until stopped/cancelled."""

    def __init__(self, **_kwargs: Any) -> None:
        self.history = _FakeHistory()
        self._stopped = False

    async def run(self, on_step_end: Any = None, max_steps: int | None = None) -> _FakeHistory:
        if on_step_end is not None:
            await on_step_end(self)
        for _ in range(10000):
            if self._stopped:
                break
            await asyncio.sleep(0.01)
        return self.history

    def stop(self) -> None:
        self._stopped = True


def test_run_agent_streams_thinking_and_action_then_done(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(bsession, "Agent", _FinishingAgent)
    monkeypatch.setattr(bsession, "ChatAnthropic", lambda **_kwargs: object())
    browser = bsession.LiveBrowser(browser_id=1)
    browser._bu_session = object()  # type: ignore[assignment]
    events: list[dict[str, Any]] = []

    async def on_event(event: dict[str, Any]) -> None:
        events.append(event)

    async def go() -> None:
        await browser.run_agent("do something", on_event)
        kinds = [e["type"] for e in events]
        assert "thinking" in kinds and "action" in kinds
        assert events[-1]["type"] == "done" and events[-1]["result"] == "all done"

    asyncio.run(go())


def test_human_take_control_preempts_a_running_task(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(bsession, "Agent", _BlockingAgent)
    monkeypatch.setattr(bsession, "ChatAnthropic", lambda **_kwargs: object())
    browser = bsession.LiveBrowser(browser_id=1)
    browser._bu_session = object()  # type: ignore[assignment]
    events: list[dict[str, Any]] = []

    async def on_event(event: dict[str, Any]) -> None:
        events.append(event)

    async def go() -> None:
        await browser.acquire("A", "Alice")
        run = asyncio.create_task(browser.run_agent("do something", on_event))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if browser._agent is not None:
                break
        assert browser._agent is not None, "agent run never started"
        await asyncio.wait_for(browser.take_control(), timeout=2.0)
        try:
            await run
        except asyncio.CancelledError:
            pass
        assert any(e["type"] == "preempted" for e in events)
        assert browser._state_tuple() == ("human", None, True)

    asyncio.run(go())


# --- HTTP layer (TestClient; run_agent stubbed) ------------------------------


def _install_fake_browser(monkeypatch: pytest.MonkeyPatch, browser_id: int = 0) -> bsession.LiveBrowser:
    runner.manager._browsers.clear()
    fake = bsession.LiveBrowser(browser_id=browser_id)
    fake._bu_session = object()  # type: ignore[assignment]
    runner.manager._browsers[browser_id] = fake
    return fake


def _stream_events(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_http_task_streams_trace_and_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake_browser(monkeypatch)

    async def fake_run_agent(self: bsession.LiveBrowser, prompt: str, on_event: Any) -> None:
        await on_event({"type": "thinking", "text": "planning"})
        await on_event({"type": "action", "text": "click"})
        await on_event({"type": "done", "result": "ok"})

    monkeypatch.setattr(bsession.LiveBrowser, "run_agent", fake_run_agent)
    client = TestClient(runner.app)
    resp = client.post(
        "/browsers/0/task",
        json={"prompt": "do it"},
        headers={"X-Mngr-Agent-Id": "A", "X-Mngr-Agent-Name": "Alice"},
    )
    assert resp.status_code == 200
    kinds = [e["type"] for e in _stream_events(resp.text)]
    assert kinds[0] == "acquired"
    assert "thinking" in kinds and "action" in kinds and "done" in kinds
    # The connection is the lease: once the task finishes, the browser is released.
    assert fake._state_tuple() == ("human", None, False)


def test_http_task_without_agent_id_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_browser(monkeypatch)
    client = TestClient(runner.app)
    resp = client.post("/browsers/0/task", json={"prompt": "do it"})
    assert resp.status_code == 400


def test_http_task_on_human_pinned_browser_reports_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake_browser(monkeypatch)

    async def pin() -> None:
        await fake.acquire("X", "X")
        await fake.take_control()  # human now holds it (pinned)

    asyncio.run(pin())
    client = TestClient(runner.app)
    resp = client.post(
        "/browsers/0/task",
        json={"prompt": "do it", "wait": False},
        headers={"X-Mngr-Agent-Id": "A", "X-Mngr-Agent-Name": "Alice"},
    )
    kinds = [e["type"] for e in _stream_events(resp.text)]
    assert kinds == ["busy_human"]


def test_http_list_browsers_shows_fleet(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_browser(monkeypatch)
    monkeypatch.setenv("BROWSER_SKIP_INSTALL_CHECK", "1")
    client = TestClient(runner.app)
    resp = client.get("/browsers")
    assert resp.status_code == 200
    ids = [b["id"] for b in resp.json()["browsers"]]
    assert 0 in ids


def test_http_release_requires_ownership(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake_browser(monkeypatch)
    asyncio.run(fake.acquire("owner", "Owner"))
    client = TestClient(runner.app)
    # A non-owner cannot free someone else's browser.
    resp = client.post("/browsers/0/release", headers={"X-Mngr-Agent-Id": "intruder"})
    assert resp.status_code == 200 and resp.json()["released"] is False
    assert fake._state_tuple() == ("agent", "owner", False)
    # The owner can.
    resp = client.post("/browsers/0/release", headers={"X-Mngr-Agent-Id": "owner"})
    assert resp.json()["released"] is True


def test_http_new_browser_blocked_until_chromium_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BROWSER_SKIP_INSTALL_CHECK", raising=False)
    monkeypatch.setattr(bsession, "_PLAYWRIGHT_MARKER", bsession.Path("/nonexistent/marker"))
    client = TestClient(runner.app)
    resp = client.post("/browsers")
    assert resp.status_code == 503


@pytest.mark.timeout(120)
def test_direct_control_state_click_is_keyless_real_chromium(monkeypatch: pytest.MonkeyPatch) -> None:
    # Direct control needs NO Anthropic key (the agent does its own reasoning; the
    # browser commands are deterministic). Drive a real page: navigate -> state ->
    # click the link -> the page changes -> re-state, all with no key set.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("MNGR_HOST_DIR", raising=False)

    async def go() -> None:
        manager = bsession.BrowserSessionManager()
        try:
            browser = await manager.create()
        except (bsession.BrowserStartupError, PlaywrightError, OSError) as e:
            pytest.skip(f"Chromium unavailable in this environment: {e}")
        try:
            nav = await browser.act_navigate("A", "Alice", "https://example.com")
            assert nav["ok"]
            # The first command newly takes the browser (the client uses this to
            # surface the pane once); later commands don't re-trigger it.
            assert nav["newly_acquired"] is True
            state = await browser.act_state("A", "Alice")
            assert state["ok"] and "example" in state["url"].lower()
            assert state.get("newly_acquired") is False
            assert browser._selector_map, "state should expose numbered elements"
            assert "controller" in state and state["controller"] == "agent"
            index = sorted(browser._selector_map)[0]
            assert (await browser.act_click("A", "Alice", index))["ok"]
            # The click navigated, so the cached indices were invalidated; re-state works.
            assert browser._selector_map == {}
            assert (await browser.act_state("A", "Alice"))["ok"]
            # Ownership holds for direct commands too: another agent is refused.
            other = await browser.act_state("B", "Bob")
            assert other["ok"] is False and other["status"] == "busy_agent"
        finally:
            await manager.shutdown()

    asyncio.run(go())


@pytest.mark.timeout(120)
def test_browser_crash_is_detected_and_reported_real_chromium(monkeypatch: pytest.MonkeyPatch) -> None:
    # Kill the live Chromium out from under the session (simulating an OS/OOM kill,
    # NOT our own close()), and confirm the daemon detects the crash and reports it
    # cleanly to the agent instead of leaking a raw CDP exception.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    async def go() -> None:
        manager = bsession.BrowserSessionManager()
        try:
            browser = await manager.create()
        except (bsession.BrowserStartupError, PlaywrightError, OSError) as e:
            pytest.skip(f"Chromium unavailable in this environment: {e}")
        try:
            assert (await browser.act_state("A", "Alice"))["ok"]  # healthy first
            # Kill Chromium directly (this is NOT close(), so _closed stays False ->
            # it looks exactly like an external crash).
            await browser._bu_session.kill()
            # The next command must report "crashed" -- via the disconnected event if
            # it already fired, otherwise via the lazy is_connected() check on failure.
            result = await browser.act_state("A", "Alice")
            assert result["ok"] is False and result["status"] == "crashed"
            assert browser._crashed is True
            assert (await browser.describe())["crashed"] is True
        finally:
            await manager.shutdown()

    asyncio.run(go())


# --- persistence: HTTP init gate + close-forgets-profile (fake browser) -------


def test_init_gate_blocks_state_changing_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    # While the fleet is still restoring, state-changing routes return 503
    # "initializing" but read-only routes stay open.
    _install_fake_browser(monkeypatch)
    runner._init_done.clear()  # simulate "still restoring"
    client = TestClient(runner.app)
    create = client.post("/browsers")
    assert create.status_code == 503 and create.json()["status"] == "initializing"
    click = client.post("/browsers/0/click", json={"index": 0}, headers={"X-Mngr-Agent-Id": "A"})
    assert click.status_code == 503 and click.json()["status"] == "initializing"
    assert client.get("/health").json()["initializing"] is True
    assert client.get("/init-status").status_code == 200
    # conftest re-sets _init_done on teardown.


def test_startup_opens_gate_even_if_restore_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # Poison-pill: a restore that raises must still open the gate (finally), never
    # wedge the daemon shut.
    async def boom(self: bsession.BrowserSessionManager) -> None:
        raise RuntimeError("restore exploded")

    monkeypatch.setattr(bsession.BrowserSessionManager, "restore", boom)
    monkeypatch.setenv("BROWSER_SKIP_INSTALL_CHECK", "1")
    runner._init_done.clear()
    with TestClient(runner.app):  # the context manager fires startup + shutdown
        pass
    assert runner._init_done.is_set()


def test_close_endpoint_deletes_profile_and_drops_from_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = bsession._profile_dir(2)
    profile.mkdir(parents=True)
    fake = _install_fake_browser(monkeypatch, browser_id=2)
    fake._bu_session = object()  # type: ignore[assignment]

    async def fake_close(self: bsession.LiveBrowser) -> None:  # avoid real Chromium teardown
        return None

    monkeypatch.setattr(bsession.LiveBrowser, "close", fake_close)
    client = TestClient(runner.app)
    resp = client.delete("/browsers/2")
    assert resp.status_code == 200
    assert not profile.exists()  # the persistent profile is forgotten on explicit close
    saved = manifest.read_manifest()
    assert saved is not None and all(e.id != 2 for e in saved.browsers)


def test_close_browser_0_keeps_its_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    # Browser 0 is the recreated-on-demand default; closing it must NOT wipe its
    # persistent profile, or the user would be silently logged out when 0 comes back.
    profile = bsession._profile_dir(0)
    profile.mkdir(parents=True)
    fake = _install_fake_browser(monkeypatch, browser_id=0)
    fake._bu_session = object()  # type: ignore[assignment]

    async def fake_close(self: bsession.LiveBrowser) -> None:
        return None

    monkeypatch.setattr(bsession.LiveBrowser, "close", fake_close)
    client = TestClient(runner.app)
    assert client.delete("/browsers/0").status_code == 200
    assert profile.exists()  # browser 0's profile (logins) is preserved across close


# --- persistence: the core promise, against real Chromium --------------------


@pytest.mark.timeout(120)
def test_profile_persists_across_manager_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    # The whole point of persistence: a cookie set in one daemon "session" is still
    # there after a restart, because the persistent user_data_dir is used IN PLACE
    # (not copied to a throwaway temp dir -- the browser_use _copy_profile trap).
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    future_expiry = 4102444800.0  # year 2100 -> a persistent (on-disk) cookie, not session-only

    async def go() -> None:
        first = bsession.BrowserSessionManager()
        try:
            await first.restore()  # first boot seeds browser 0
        except (bsession.BrowserStartupError, PlaywrightError, OSError) as e:
            pytest.skip(f"Chromium unavailable in this environment: {e}")
        try:
            browser = first.get(0)
            assert (await browser.act_navigate("A", "Alice", "https://example.com"))["ok"]
            # Anti-_copy_profile tripwire: the live profile is our persistent dir, NOT a temp copy.
            assert str(_profile_dir_for(0)) == str(browser._bu_session.browser_profile.user_data_dir)
            await browser._context.add_cookies(
                [{"name": "fleet_test", "value": "persisted", "url": "https://example.com", "expires": future_expiry}]
            )
            await first._save_manifest()
        finally:
            await first.shutdown()  # clean stop flushes the profile to disk

        second = bsession.BrowserSessionManager()
        await second.restore()
        try:
            cookies = await second.get(0)._context.cookies("https://example.com")
            assert any(c["name"] == "fleet_test" and c["value"] == "persisted" for c in cookies)
        finally:
            await second.shutdown()

    asyncio.run(go())


def _profile_dir_for(browser_id: int):
    # Helper kept tiny so the tripwire reads clearly above.
    return bsession._profile_dir(browser_id)
