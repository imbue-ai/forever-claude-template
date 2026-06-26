"""Integration tests for the browser fleet.

Three kinds:
- A real headless-Chromium test of the steel-style path (spawn -> CDP screencast
  frames -> input dispatch -> open a 2nd tab -> active-tab follow). It skips when
  Chromium isn't installed (CI runners without the deferred-install), so it never
  fails for lack of a browser; it runs on a host/compute that has Chromium.
- A browser-use-free test of the run-agent event stream + human take-control
  preemption, with Agent/ChatAnthropic mocked so it runs everywhere.
- HTTP-layer tests of the fleet endpoints (list / task stream / release / cap)
  via Flask's test client, with run_agent stubbed (no LLM, no browser). These reach
  session.py coroutines through the bridge loop (started once by the conftest fixture).
- A boot-a-server integration test of the cast WebSocket + disconnect-as-lease over a
  real socket, against a fake session (no real Chromium).
"""

import asyncio
import json
import os
import queue
import socket
import threading
import time
from typing import Any

import pytest
import simple_websocket
from browser import manifest, runner
from browser import session as bsession
from browser.wsgi import make_threaded_server
from playwright.async_api import Error as PlaywrightError

# Real Chromium launches but its CDP connection never completes on the GitHub Actions
# runner -- the launch hangs (manifesting as a pytest-timeout + a NoneType CDP-session
# error), even though `playwright install` put the binary there and even with the sandbox
# off. It is not a product issue: the fleet runs fine on real workspaces (docker / Lima /
# cloud, all verified). So skip the real-Chromium tests in GH CI; they still run locally
# and on offload, where a real browser actually comes up.
_SKIP_REAL_CHROMIUM_IN_GH_CI = pytest.mark.skipif(
    os.environ.get("GITHUB_ACTIONS") == "true",
    reason="real Chromium can't start under the GitHub Actions runner; runs locally / on offload",
)


def _drain_cast_queue(cast_queue: Any) -> tuple[list[str], list[dict[str, Any]]]:
    """Split a cast queue's buffered JSON strings into frames vs other events."""
    frames: list[str] = []
    events: list[dict[str, Any]] = []
    drained = False
    while not drained:
        try:
            obj = json.loads(cast_queue.get_nowait())
        except queue.Empty:
            drained = True
            continue
        if obj.get("type") == "frame":
            frames.append(obj["data"])
        else:
            events.append(obj)
    return frames, events


@_SKIP_REAL_CHROMIUM_IN_GH_CI
@pytest.mark.timeout(120)  # real-Chromium cold-start + nav exceeds the global 10s locally/offload
def test_live_browser_streams_and_accepts_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROWSER_HEADLESS", "1")

    async def go() -> None:
        manager = bsession.BrowserSessionManager()
        try:
            session = await manager.create()
        except (bsession.BrowserStartupError, PlaywrightError, OSError) as e:
            pytest.skip(f"Chromium unavailable in this environment: {e}")
        try:
            cast_queue = await session.register_cast_queue()
            await session.handle_cast_message({"type": "navigate", "url": "https://example.com"})
            frames: list[str] = []
            for _ in range(20):
                await asyncio.sleep(0.5)
                more_frames, _ = _drain_cast_queue(cast_queue)
                frames += more_frames
                if frames:
                    break
            assert frames, "expected at least one screencast frame"

            # Human input dispatch must not raise against the live target.
            await session.handle_cast_message(
                {"type": "mouse", "event": {"type": "mouseMoved", "x": 50, "y": 50, "button": "none"}}
            )

            # Open a second tab and confirm the view follows it (active switches).
            await session.handle_cast_message({"type": "tab", "action": "new", "url": "https://example.org"})
            await asyncio.sleep(2)
            _, events = _drain_cast_queue(cast_queue)
            tab_events = [e for e in events if e.get("type") == "tabs"]
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
    browser = bsession.LiveBrowser(browser_id="b1")
    browser._bu_session = object()  # type: ignore[assignment]
    events: list[dict[str, Any]] = []

    async def on_event(event: dict[str, Any]) -> None:
        events.append(event)

    async def go() -> None:
        # run_agent re-checks ownership under the control lock before driving, so the
        # browser must be acquired by this agent first (mirrors the task endpoint).
        await browser.acquire("A", "Alice")
        await browser.run_agent("A", "do something", on_event)
        kinds = [e["type"] for e in events]
        assert "thinking" in kinds and "action" in kinds
        assert events[-1]["type"] == "done" and events[-1]["result"] == "all done"

    asyncio.run(go())


def test_human_take_control_preempts_a_running_task(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(bsession, "Agent", _BlockingAgent)
    monkeypatch.setattr(bsession, "ChatAnthropic", lambda **_kwargs: object())
    browser = bsession.LiveBrowser(browser_id="b1")
    browser._bu_session = object()  # type: ignore[assignment]
    events: list[dict[str, Any]] = []

    async def on_event(event: dict[str, Any]) -> None:
        events.append(event)

    async def go() -> None:
        await browser.acquire("A", "Alice")
        run = asyncio.create_task(browser.run_agent("A", "do something", on_event))
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


def test_run_agent_aborts_if_control_lost_before_it_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    # RACE 1: the task endpoint acquires in one submitted coroutine, then submits
    # run_agent SEPARATELY. If a human take_control lands in that gap, run_agent must
    # NOT drive the human's browser: it re-checks ownership under the control lock and
    # aborts with `lost_control`, never constructing/registering the agent.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(bsession, "Agent", _BlockingAgent)
    monkeypatch.setattr(bsession, "ChatAnthropic", lambda **_kwargs: object())
    browser = bsession.LiveBrowser(browser_id="b1")
    browser._bu_session = object()  # type: ignore[assignment]
    events: list[dict[str, Any]] = []

    async def on_event(event: dict[str, Any]) -> None:
        events.append(event)

    async def go() -> None:
        await browser.acquire("A", "Alice")
        # The human preempts in the gap -- before run_agent is even scheduled.
        await browser.take_control()
        assert browser._state_tuple() == ("human", None, True)
        await browser.run_agent("A", "do something", on_event)
        # run_agent declined to drive: it emitted lost_control and registered no handle,
        # so the human still owns a browser no agent ever touched.
        assert events == [{"type": "lost_control", **browser._control_state()}]
        assert browser._agent is None and browser._agent_task is None
        assert browser._state_tuple() == ("human", None, True)

    asyncio.run(go())


# --- HTTP layer (Flask test client; run_agent stubbed) -----------------------


def _install_fake_browser(monkeypatch: pytest.MonkeyPatch, browser_id: str = "alex-smith") -> bsession.LiveBrowser:
    runner.manager._browsers.clear()
    fake = bsession.LiveBrowser(browser_id=browser_id)
    fake._bu_session = object()  # type: ignore[assignment]
    runner.manager._browsers[browser_id] = fake
    return fake


def _stream_events(text: str) -> list[dict[str, Any]]:
    # Drop heartbeat pings: the Flask NDJSON generators emit a `ping` every ~0.5s of
    # idle so a dead client surfaces as a broken-pipe write; they aren't trace events.
    events = [json.loads(line) for line in text.splitlines() if line.strip()]
    return [e for e in events if e.get("type") != "ping"]


def test_http_task_streams_trace_and_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake_browser(monkeypatch)

    async def fake_run_agent(self: bsession.LiveBrowser, agent_id: str, prompt: str, on_event: Any) -> None:
        await on_event({"type": "thinking", "text": "planning"})
        await on_event({"type": "action", "text": "click"})
        await on_event({"type": "done", "result": "ok"})

    monkeypatch.setattr(bsession.LiveBrowser, "run_agent", fake_run_agent)
    client = runner.application.test_client()
    resp = client.post(
        "/browsers/alex-smith/task",
        json={"prompt": "do it"},
        headers={"X-Mngr-Agent-Id": "A", "X-Mngr-Agent-Name": "Alice"},
    )
    assert resp.status_code == 200
    kinds = [e["type"] for e in _stream_events(resp.get_data(as_text=True))]
    assert kinds[0] == "acquired"
    assert "thinking" in kinds and "action" in kinds and "done" in kinds
    # The connection is the lease: once the task finishes, the browser is released.
    assert fake._state_tuple() == ("human", None, False)


def test_http_task_without_agent_id_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_browser(monkeypatch)
    client = runner.application.test_client()
    resp = client.post("/browsers/alex-smith/task", json={"prompt": "do it"})
    assert resp.status_code == 400


def test_http_task_on_human_pinned_browser_reports_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake_browser(monkeypatch)

    async def pin() -> None:
        await fake.acquire("X", "X")
        await fake.take_control()  # human now holds it (pinned)

    asyncio.run(pin())
    client = runner.application.test_client()
    resp = client.post(
        "/browsers/alex-smith/task",
        json={"prompt": "do it", "wait": False},
        headers={"X-Mngr-Agent-Id": "A", "X-Mngr-Agent-Name": "Alice"},
    )
    kinds = [e["type"] for e in _stream_events(resp.get_data(as_text=True))]
    assert kinds == ["busy_human"]


def test_http_list_browsers_shows_fleet(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_browser(monkeypatch)
    monkeypatch.setenv("BROWSER_SKIP_INSTALL_CHECK", "1")
    client = runner.application.test_client()
    resp = client.get("/browsers")
    assert resp.status_code == 200
    ids = [b["id"] for b in resp.get_json()["browsers"]]
    assert "alex-smith" in ids


def test_http_release_requires_ownership(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake_browser(monkeypatch)
    asyncio.run(fake.acquire("owner", "Owner"))
    client = runner.application.test_client()
    # A non-owner cannot free someone else's browser.
    resp = client.post("/browsers/alex-smith/release", headers={"X-Mngr-Agent-Id": "intruder"})
    assert resp.status_code == 200 and resp.get_json()["released"] is False
    assert fake._state_tuple() == ("agent", "owner", False)
    # The owner can.
    resp = client.post("/browsers/alex-smith/release", headers={"X-Mngr-Agent-Id": "owner"})
    assert resp.get_json()["released"] is True


def test_http_new_browser_blocked_until_chromium_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BROWSER_SKIP_INSTALL_CHECK", raising=False)
    monkeypatch.setattr(bsession, "_PLAYWRIGHT_MARKER", bsession.Path("/nonexistent/marker"))
    client = runner.application.test_client()
    resp = client.post("/browsers")
    assert resp.status_code == 503


@_SKIP_REAL_CHROMIUM_IN_GH_CI
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


@_SKIP_REAL_CHROMIUM_IN_GH_CI
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


def test_init_gate_blocks_drive_verbs_but_not_read_only_or_create(monkeypatch: pytest.MonkeyPatch) -> None:
    # While the fleet is still restoring, the DRIVE verbs (click/task/...) return 503
    # "initializing", but read-only routes (ls/state/health) AND create stay open --
    # the locked "init must not block create" decision (a create queues behind the
    # serialized restore on the manager lock).
    _install_fake_browser(monkeypatch)
    runner._init_done.clear()  # simulate "still restoring"
    client = runner.application.test_client()
    # A drive verb on an existing browser is still gated during init.
    click = client.post("/browsers/alex-smith/click", json={"index": 0}, headers={"X-Mngr-Agent-Id": "A"})
    assert click.status_code == 503 and click.get_json()["status"] == "initializing"
    # Read-only routes stay open.
    assert client.get("/browsers").status_code == 200
    assert client.get("/health").get_json()["initializing"] is True
    assert client.get("/init-status").status_code == 200
    # Create is NOT init-gated: it reaches manager.create (stubbed here to avoid a real
    # launch) and returns 200, NOT 503.
    monkeypatch.setenv("BROWSER_SKIP_INSTALL_CHECK", "1")

    async def fake_create(self: bsession.BrowserSessionManager, name: str | None = None) -> bsession.LiveBrowser:
        created = bsession.LiveBrowser(browser_id=name or "morgan-lee")
        self._browsers[created.browser_id] = created
        return created

    monkeypatch.setattr(bsession.BrowserSessionManager, "create", fake_create)
    create = client.post("/browsers")
    assert create.status_code == 200 and create.get_json()["name"] == "morgan-lee"
    # conftest re-sets _init_done on teardown.


def test_startup_opens_gate_even_if_restore_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # Poison-pill: a restore that raises must still open the gate (finally), never
    # wedge the daemon shut. _startup runs on the bridge loop (as in create_app).
    async def boom(self: bsession.BrowserSessionManager) -> None:
        raise RuntimeError("restore exploded")

    monkeypatch.setattr(bsession.BrowserSessionManager, "restore", boom)
    monkeypatch.setenv("BROWSER_SKIP_INSTALL_CHECK", "1")
    runner._init_done.clear()
    runner.bridge.run(runner._startup())  # the loop runs the same startup coroutine
    assert runner._init_done.is_set()


def test_close_endpoint_deletes_profile_and_drops_from_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every browser is created on demand (no permanent default), so closing one ALWAYS
    # forgets its persistent profile and drops it from the manifest.
    profile = bsession._profile_dir("riley-jones")
    profile.mkdir(parents=True)
    fake = _install_fake_browser(monkeypatch, browser_id="riley-jones")
    fake._bu_session = object()  # type: ignore[assignment]

    async def fake_close(self: bsession.LiveBrowser) -> None:  # avoid real Chromium teardown
        return None

    monkeypatch.setattr(bsession.LiveBrowser, "close", fake_close)
    client = runner.application.test_client()
    resp = client.delete("/browsers/riley-jones")
    assert resp.status_code == 200
    assert not profile.exists()  # the persistent profile is forgotten on explicit close
    saved = manifest.read_manifest()
    assert saved is not None and all(e.id != "riley-jones" for e in saved.browsers)


# --- persistence: the core promise, against real Chromium --------------------


@_SKIP_REAL_CHROMIUM_IN_GH_CI
@pytest.mark.timeout(120)
def test_profile_persists_across_manager_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    # The whole point of persistence: a cookie set in one daemon "session" is still
    # there after a restart, because the persistent user_data_dir is used IN PLACE
    # (not copied to a throwaway temp dir -- the browser_use _copy_profile trap).
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    future_expiry = 4102444800.0  # year 2100 -> a persistent (on-disk) cookie, not session-only

    async def go() -> None:
        first = bsession.BrowserSessionManager()
        await first.restore()  # fresh workspace -> EMPTY fleet (no default browser)
        try:
            # Every browser is created on demand now; create one and remember its name.
            try:
                browser = await first.create()
            except (bsession.BrowserStartupError, PlaywrightError, OSError) as e:
                pytest.skip(f"Chromium unavailable in this environment: {e}")
            name = browser.browser_id
            assert (await browser.act_navigate("A", "Alice", "https://example.com"))["ok"]
            # Anti-_copy_profile tripwire: the live profile is our persistent dir, NOT a temp copy.
            assert str(_profile_dir_for(name)) == str(browser._bu_session.browser_profile.user_data_dir)
            first_context = browser._context
            assert first_context is not None, "context should be live after a successful navigate"
            await first_context.add_cookies(
                [{"name": "fleet_test", "value": "persisted", "url": "https://example.com", "expires": future_expiry}]
            )
            await first._save_manifest()
        finally:
            await first.shutdown()  # clean stop flushes the profile to disk

        second = bsession.BrowserSessionManager()
        await second.restore()  # the saved browser comes back by name
        try:
            second_context = second.get(name)._context
            assert second_context is not None, "context should be live after restore"
            cookies = await second_context.cookies("https://example.com")
            assert any(c.get("name") == "fleet_test" and c.get("value") == "persisted" for c in cookies)
        finally:
            await second.shutdown()

    asyncio.run(go())


def _profile_dir_for(browser_id: str):
    # Helper kept tiny so the tripwire reads clearly above.
    return bsession._profile_dir(browser_id)


# --- boot-a-server: cast WS dual-direction + disconnect-as-lease over a real socket ---
# These exercise the real Werkzeug threaded server + socket path that the Flask test
# client (in-process GeneratorExit) does NOT cover -- so the disconnect-detection-via-
# heartbeat-write contract is verified empirically, not assumed.


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _BootedServer:
    """Boot runner.application on an ephemeral port in a background thread."""

    def __init__(self) -> None:
        self.port = _free_port()
        self._server = make_threaded_server("127.0.0.1", self.port, runner.application)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "_BootedServer":
        self._thread.start()
        # Wait for the listener to accept connections.
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        return self

    def __exit__(self, *_exc: Any) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _ws_recv_json(ws: Any, timeout: float) -> dict[str, Any]:
    """Receive one WebSocket message and parse it as JSON.

    ``ws.receive`` returns ``str | bytes | None`` (None on a closed/timed-out socket);
    asserting it's a payload narrows the type for ``json.loads`` and fails loudly if the
    socket dropped when a message was expected."""
    payload = ws.receive(timeout=timeout)
    assert payload is not None, "expected a WebSocket message but the socket returned nothing"
    return json.loads(payload)


@pytest.mark.timeout(30)
def test_cast_ws_streams_control_and_take_control_flips_ownership(monkeypatch: pytest.MonkeyPatch) -> None:
    # The load-bearing WS inversion: the loop fans frames/control out onto the cast
    # queue and the Flask thread sends them; inbound take_control is read on a second
    # thread and dispatched to the loop. No real Chromium -- a fake session suffices.
    fake = _install_fake_browser(monkeypatch)
    fake._context = None  # _tab_list -> [] without Chromium
    with _BootedServer() as server:
        ws = simple_websocket.Client(f"ws://127.0.0.1:{server.port}/browsers/alex-smith/cast")
        try:
            # The viewer's first messages are the deterministic initial sync.
            first = _ws_recv_json(ws, timeout=5)
            assert first["type"] == "control" and first["owner"] == "human"
            # Inbound take_control flips ownership on the loop (human pins).
            ws.send(json.dumps({"type": "take_control"}))
            assert _wait_until(lambda: fake._state_tuple() == ("human", None, True))
            # The control flip is broadcast back out over the same socket.
            saw_pin = False
            for _ in range(20):
                msg = _ws_recv_json(ws, timeout=2)
                if msg.get("type") == "control" and msg.get("human_pinned") is True:
                    saw_pin = True
                    break
            assert saw_pin, "expected a pinned-control broadcast after take_control"
        finally:
            ws.close()
        # Disconnect unregisters the cast queue on the loop (cleanup ran).
        assert _wait_until(lambda: fake._cast_queues == [])


@pytest.mark.timeout(30)
def test_hold_releases_the_lease_when_the_client_socket_dies(monkeypatch: pytest.MonkeyPatch) -> None:
    # Disconnect-as-lease over a REAL socket: POST /hold, confirm the agent owns the
    # browser, then hard-close the socket. The heartbeat write fails -> the generator's
    # finally runs -> the lease is released. This is the contract the in-process test
    # client cannot exercise (it never fails a real socket write).
    fake = _install_fake_browser(monkeypatch)
    fake._context = None
    with _BootedServer() as server:
        conn = socket.create_connection(("127.0.0.1", server.port), timeout=5)
        request = (
            "POST /browsers/alex-smith/hold HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{server.port}\r\n"
            "X-Mngr-Agent-Id: A\r\n"
            "X-Mngr-Agent-Name: Alice\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: 2\r\n"
            "\r\n"
            "{}"
        )
        conn.sendall(request.encode())
        # Read until we see the "held" line, confirming the agent acquired the lease.
        conn.settimeout(5)
        assert _wait_until(lambda: fake._state_tuple() == ("agent", "A", False))
        buffered = b""
        for _ in range(20):
            buffered += conn.recv(4096)
            if b"held" in buffered:
                break
        assert b"held" in buffered
        # Hard-close the client. The next heartbeat write fails, GeneratorExit runs the
        # finally, and the lease is released back to the human.
        conn.close()
        assert _wait_until(lambda: fake._state_tuple() == ("human", None, False), timeout=10.0)
