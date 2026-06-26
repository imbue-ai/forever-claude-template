import asyncio
import json
import queue
import time
from pathlib import Path
from typing import Any

import pytest
from browser import manifest
from browser import session as bsession


async def _noop_wake(self: bsession.LiveBrowser, agent_id: str, agent_name: str | None) -> None:
    """Stand-in for ``_wake_agent`` in tests: skip the real ``mngr message`` subprocess."""


def _pop_json(cast_queue: "queue.Queue[str | None]") -> dict[str, Any]:
    """Pop the next cast-queue payload and parse it as JSON.

    A cast queue holds JSON strings, with ``None`` reserved as the shutdown sentinel
    (never enqueued in these tests). Asserting it isn't ``None`` narrows the type for
    ``json.loads`` and explodes loudly if a sentinel ever leaked in."""
    payload = cast_queue.get_nowait()
    assert payload is not None, "unexpected shutdown sentinel on the cast queue"
    return json.loads(payload)


# --- env / key helpers (unchanged) -------------------------------------------


def test_parse_env_file_handles_quotes_and_comments() -> None:
    text = '# comment\nANTHROPIC_API_KEY=sk-ant-123\nQUOTED="a b c"\nEMPTY=\n'
    parsed = bsession._parse_env_file(text)
    assert parsed["ANTHROPIC_API_KEY"] == "sk-ant-123"
    assert parsed["QUOTED"] == "a b c"
    assert parsed["EMPTY"] == ""


def test_resolve_key_prefers_process_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-proc")
    assert bsession.resolve_anthropic_key() == "sk-proc"


def test_resolve_key_falls_back_to_host_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (tmp_path / "env").write_text("ANTHROPIC_API_KEY=sk-host\n")
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    assert bsession.resolve_anthropic_key() == "sk-host"


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
    monkeypatch.delenv("BROWSER_SKIP_INSTALL_CHECK", raising=False)
    play = tmp_path / "done.playwright"
    monkeypatch.setattr(bsession, "_PLAYWRIGHT_MARKER", play)
    ready, _ = bsession.deferred_install_ready()
    assert ready is False
    play.write_text("")
    ready, reason = bsession.deferred_install_ready()
    assert ready is True
    assert reason == "ready"


# --- ownership state machine (no browser needed) -----------------------------


class _FakeCDP:
    def __init__(self) -> None:
        self.sends: list[tuple[str, Any]] = []

    async def send(self, method: str, params: Any = None) -> dict[str, Any]:
        self.sends.append((method, params))
        return {}


def test_acquire_release_is_compare_and_set() -> None:
    browser = bsession.LiveBrowser(browser_id="b1")

    async def go() -> None:
        assert await browser.acquire("A", "Alice") == "acquired"
        assert browser._state_tuple() == ("agent", "A", False)
        # A second agent can't grab it; with --no-wait it fails fast.
        assert await browser.acquire("B", "Bob", wait=False) == "busy_agent"
        # The same agent re-acquiring is idempotent.
        assert await browser.acquire("A") == "acquired"
        # Only the owner can release; a double / non-owner release is a safe no-op.
        assert await browser.release("A") is True
        assert browser._state_tuple() == ("human", None, False)
        assert await browser.release("A") is False
        assert await browser.release("B") is False

    asyncio.run(go())


def test_input_gating_follows_controller() -> None:
    browser = bsession.LiveBrowser(browser_id="b1")
    cdp = _FakeCDP()
    browser._active_cdp = cdp  # type: ignore[assignment]

    async def go() -> None:
        # Human (resting): a mouse event is dispatched to the browser.
        await browser.handle_cast_message({"type": "mouse", "event": {"type": "mouseMoved"}})
        assert any(m == "Input.dispatchMouseEvent" for m, _ in cdp.sends)
        cdp.sends.clear()
        # Agent in control: human input is dropped (the input/control TOCTOU guard).
        await browser.acquire("A")
        assert not browser._input_enabled.is_set()
        await browser.handle_cast_message({"type": "mouse", "event": {"type": "mouseMoved"}})
        await browser.handle_cast_message({"type": "tab", "action": "new"})
        assert cdp.sends == []
        # Released back to the human: input flows again.
        await browser.release("A")
        assert browser._input_enabled.is_set()
        await browser.handle_cast_message({"type": "mouse", "event": {"type": "mouseMoved"}})
        assert any(m == "Input.dispatchMouseEvent" for m, _ in cdp.sends)

    asyncio.run(go())


def test_take_control_preempts_pins_and_reclaim_resumes() -> None:
    browser = bsession.LiveBrowser(browser_id="b1")

    async def go() -> None:
        await browser.acquire("A", "Alice")
        # Human take-control always wins: pinned human, input re-enabled.
        assert await browser.take_control() is True
        assert browser._state_tuple() == ("human", None, True)
        assert browser._input_enabled.is_set()
        # While pinned, agents are locked out -- even with wait they get busy_human.
        assert await browser.acquire("B", "Bob", wait=False) == "busy_human"
        assert await browser.acquire("B", "Bob", wait=True, max_wait=0.1) == "busy_human"
        # Only an explicit reclaim (the human told the agent to resume) takes it back.
        assert await browser.acquire("B", "Bob", reclaim=True) == "acquired"
        assert browser._state_tuple() == ("agent", "B", False)

    asyncio.run(go())


def test_enqueue_on_busy_queues_for_resume_and_wakes_on_handback(monkeypatch: pytest.MonkeyPatch) -> None:
    # Direct-control handoff: a human takes control, the agent's next command is
    # rejected (busy_human) and the agent is queued to resume. When the human hands
    # back, the queued agent is granted control and messaged to resume.
    woken: list[str | None] = []

    async def fake_wake(self: bsession.LiveBrowser, agent_id: str, agent_name: str | None) -> None:
        woken.append(agent_name)

    monkeypatch.setattr(bsession.LiveBrowser, "_wake_agent", fake_wake)
    browser = bsession.LiveBrowser(browser_id="b1")

    async def go() -> None:
        await browser.acquire("A", "Alice")
        await browser.take_control()  # human grabs it, pins (active)
        # A's next direct command is rejected AND it's queued to resume.
        assert await browser.acquire("A", "Alice", wait=False, enqueue_on_busy=True) == "busy_human"
        assert browser._waiting_names() == ["Alice"]
        # Human hands back -> A is granted control and woken to resume.
        assert await browser.return_to_agents() is True
        assert browser._state_tuple() == ("agent", "A", False)
        assert browser._waiting_names() == []  # dequeued on grant
        await asyncio.sleep(0)  # let the wake task run
        assert woken == ["Alice"]

    asyncio.run(go())


def test_agent_in_both_queues_is_not_re_granted_after_it_finishes(monkeypatch: pytest.MonkeyPatch) -> None:
    # An agent can land in BOTH queues: a rejected direct command queues it for resume,
    # then it runs an explicit blocking acquire and parks in the wait queue. When the
    # wait-queue grant fires it must be removed from the resume queue too, or releasing
    # later would spuriously re-grant the freed browser to the (now-done) agent.
    woken: list[str | None] = []

    async def fake_wake(self: bsession.LiveBrowser, agent_id: str, agent_name: str | None) -> None:
        woken.append(agent_name)

    monkeypatch.setattr(bsession.LiveBrowser, "_wake_agent", fake_wake)
    browser = bsession.LiveBrowser(browser_id="b1")

    async def go() -> None:
        await browser.acquire("A", "Alice")  # A holds it
        # B's direct command is rejected by A -> B queued for resume.
        assert await browser.acquire("B", "Bob", wait=False, enqueue_on_busy=True) == "busy_agent"
        assert browser._waiting_names() == ["Bob"]
        # B then parks in the connection-bound wait queue for the same browser.
        b_wait = asyncio.create_task(browser.acquire("B", "Bob", wait=True, max_wait=2.0))
        await asyncio.sleep(0)
        # A releases -> B is granted from the wait queue AND cleared from resume queue.
        await browser.release("A")
        assert await b_wait == "acquired"
        assert browser._state_tuple() == ("agent", "B", False)
        assert browser._waiting_names() == []  # not lingering in the resume queue
        # B finishes. Releasing must NOT re-grant to B (it would, if B were still queued).
        await browser.release("B")
        assert browser._state_tuple() == ("human", None, False)
        assert woken == []  # no spurious "handed back to you" wake

    asyncio.run(go())


def test_human_pin_is_sticky_with_no_idle_yield(monkeypatch: pytest.MonkeyPatch) -> None:
    # A human take-control is STICKY: it holds until the human explicitly hands back,
    # with no grace/idle yield -- even with an agent queued to resume. (A human can walk
    # away mid-CAPTCHA and the browser is never moved out from under them.)
    monkeypatch.setattr(bsession.LiveBrowser, "_wake_agent", _noop_wake)
    browser = bsession.LiveBrowser(browser_id="b1")

    async def go() -> None:
        await browser.acquire("A", "Alice")
        await browser.take_control()  # human pins
        assert await browser.acquire("A", "Alice", wait=False, enqueue_on_busy=True) == "busy_human"
        assert browser._waiting_names() == ["Alice"]
        # The only keepalive sweeps left act on agent ownership, never a human pin.
        assert await browser._sweep_unclaimed_grant() is False
        assert await browser._sweep_idle_lease() is False
        assert browser._state_tuple() == ("human", None, True)  # still pinned to the human
        assert await browser.acquire("A", "Alice", wait=False) == "busy_human"  # still locked out
        # Only an explicit hand-back returns it -- and the queued agent then resumes.
        assert await browser.return_to_agents() is True
        assert browser._state_tuple() == ("agent", "A", False)

    asyncio.run(go())


def test_resting_human_is_free_for_the_next_agent() -> None:
    # A *resting* human (controller=human, not pinned -- a fresh browser, or one an
    # agent's idle-lease released) is free: the next agent's command just takes it.
    browser = bsession.LiveBrowser(browser_id="b1")

    async def go() -> None:
        assert browser._state_tuple() == ("human", None, False)  # fresh = resting/free
        assert await browser.acquire("A", "Alice", wait=False) == "acquired"
        await browser.release("A")  # back to resting (not pinned)
        assert browser._state_tuple() == ("human", None, False)
        assert await browser.acquire("B", "Bob", wait=False) == "acquired"  # taken freely

    asyncio.run(go())


def test_handoff_to_human_fronts_resume_queue_and_announces(monkeypatch: pytest.MonkeyPatch) -> None:
    # An agent that hits a CAPTCHA hands the browser to the HUMAN (pinned, NOT the next
    # queued agent) and jumps to the FRONT of the resume queue, so it resumes first when
    # the human hands back. A distinct handoff_request is broadcast for the viewer.
    monkeypatch.setattr(bsession.LiveBrowser, "_wake_agent", _noop_wake)
    casts: list[dict] = []

    def fake_broadcast(self: bsession.LiveBrowser, message: dict) -> None:
        casts.append(message)

    monkeypatch.setattr(bsession.LiveBrowser, "_broadcast", fake_broadcast)
    browser = bsession.LiveBrowser(browser_id="b1")

    async def go() -> None:
        await browser.acquire("A", "Alice")
        # B is already queued behind A (its direct command was rejected).
        assert await browser.acquire("B", "Bob", wait=False, enqueue_on_busy=True) == "busy_agent"
        assert browser._waiting_names() == ["Bob"]
        # A hands off -> human pinned, A jumps to the FRONT of the queue (ahead of B).
        assert await browser.handoff("A", "Alice", "solve the CAPTCHA") is True
        assert browser._state_tuple() == ("human", None, True)
        assert browser._waiting_names() == ["Alice", "Bob"]
        announced = [m for m in casts if m.get("type") == "handoff_request"]
        assert announced and announced[-1]["reason"] == "solve the CAPTCHA"
        assert announced[-1]["agent_name"] == "Alice"
        # Hand-back goes to the requester (A), not B.
        assert await browser.return_to_agents() is True
        assert browser._state_tuple() == ("agent", "A", False)

    asyncio.run(go())


def test_handoff_is_a_noop_when_the_caller_does_not_hold_it() -> None:
    # Only the current owner can hand off; a stale/wrong caller changes nothing.
    browser = bsession.LiveBrowser(browser_id="b1")

    async def go() -> None:
        await browser.acquire("A", "Alice")
        assert await browser.handoff("B", "Bob", "x") is False  # B never owned it
        assert browser._state_tuple() == ("agent", "A", False)  # unchanged
        await browser.take_control()  # a human now holds it
        assert await browser.handoff("A", "Alice", "x") is False  # A no longer owns it
        assert browser._state_tuple() == ("human", None, True)

    asyncio.run(go())


def test_should_disable_sandbox_when_running_as_root(monkeypatch: pytest.MonkeyPatch) -> None:
    # Chromium can't sandbox as root, so we disable it when euid==0 (the minds-workspace
    # case) and keep it for a non-root runtime (local dev, where the sandbox works).
    monkeypatch.setattr(bsession.os, "geteuid", lambda: 0)
    assert bsession._should_disable_sandbox() is True
    monkeypatch.setattr(bsession.os, "geteuid", lambda: 501)
    assert bsession._should_disable_sandbox() is False


class _FakeBuSession:
    """A stand-in for browser-use's BrowserSession: its ``start`` fails when the sandbox
    is on (mimicking a runtime that can't sandbox), so we can exercise the launch paths."""

    def __init__(self, chromium_sandbox: bool) -> None:
        self.chromium_sandbox = chromium_sandbox

    async def start(self) -> None:
        if self.chromium_sandbox:
            raise bsession.BrowserStartupError("Running as root without --no-sandbox is not supported.")


def _patch_build(monkeypatch: pytest.MonkeyPatch, attempts: list[bool]) -> None:
    def build(self: bsession.LiveBrowser, profile_dir: Path, chromium_path: str, *, chromium_sandbox: bool) -> Any:
        attempts.append(chromium_sandbox)
        return _FakeBuSession(chromium_sandbox)

    monkeypatch.setattr(bsession.LiveBrowser, "_build_bu_session", build)


def test_root_launches_with_sandbox_off_on_the_first_try(monkeypatch: pytest.MonkeyPatch) -> None:
    # As root (Lima / any minds workspace) the sandbox is off from the start -- no doomed
    # sandboxed attempt that browser-use would turn into a 30s hang (the 504 cause).
    attempts: list[bool] = []
    _patch_build(monkeypatch, attempts)
    monkeypatch.setattr(bsession.os, "geteuid", lambda: 0)
    browser = bsession.LiveBrowser(browser_id="b0")

    async def go() -> None:
        session = await browser._start_bu_session(Path("/tmp/x"), "/usr/bin/chromium")
        assert attempts == [False]  # one attempt, sandbox already off
        assert isinstance(session, _FakeBuSession) and session.chromium_sandbox is False

    asyncio.run(go())


def test_nonroot_retries_without_sandbox_when_a_sandboxed_launch_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # A non-root runtime keeps the sandbox, but if that launch fails we retry once with it
    # off (the only thing the retry changes) -- the backstop for a non-root no-sandbox env.
    attempts: list[bool] = []
    _patch_build(monkeypatch, attempts)
    monkeypatch.setattr(bsession.os, "geteuid", lambda: 501)
    browser = bsession.LiveBrowser(browser_id="b0")

    async def go() -> None:
        session = await browser._start_bu_session(Path("/tmp/x"), "/usr/bin/chromium")
        assert attempts == [True, False]  # sandbox on (fails) -> retried off (succeeds)
        assert isinstance(session, _FakeBuSession) and session.chromium_sandbox is False

    asyncio.run(go())


def test_unclaimed_grant_passes_to_next_waiter(monkeypatch: pytest.MonkeyPatch) -> None:
    # An agent granted the browser from the resume queue but that never sends a
    # command (interrupted/killed) has its grant revoked after the claim window, so
    # the browser doesn't sit idle on a no-show.
    monkeypatch.setattr(bsession.LiveBrowser, "_wake_agent", _noop_wake)
    browser = bsession.LiveBrowser(browser_id="b1")

    async def go() -> None:
        await browser.acquire("A", "Alice")
        await browser.take_control()
        await browser.acquire("A", "Alice", wait=False, enqueue_on_busy=True)  # A queues
        await browser.return_to_agents()  # A granted + (fake) woken, but never sends a command
        assert browser._state_tuple() == ("agent", "A", False)
        assert browser._granted_at  # claim window armed (A hasn't sent a command)
        # Simulate the claim window elapsing with no command from A (lease stays older
        # than the grant -> A never claimed): the sweep revokes and frees the browser.
        overdue = time.monotonic() - bsession._CLAIM_WINDOW - 1
        browser._granted_at = overdue
        browser._lease_touched_at = overdue - 1
        assert await browser._sweep_unclaimed_grant() is True
        assert browser._state_tuple() == ("human", None, False)

    asyncio.run(go())


def test_return_to_agents_only_unpins_a_pinned_human() -> None:
    browser = bsession.LiveBrowser(browser_id="b1")

    async def go() -> None:
        # No-op when an agent owns it (can't yank a browser from an agent this way).
        await browser.acquire("A")
        assert await browser.return_to_agents() is False
        await browser.release("A")
        # No-op when already a free human.
        assert await browser.return_to_agents() is False
        # Un-pins a human who took control.
        await browser.acquire("A")
        await browser.take_control()
        assert await browser.return_to_agents() is True
        assert browser._state_tuple() == ("human", None, False)

    asyncio.run(go())


def test_take_control_cancels_the_running_task_without_deadlock() -> None:
    # The displaced run's finally re-enters the state machine; the cancel happens
    # OUTSIDE the control lock, so there is no lock cycle (the audit's worst case).
    browser = bsession.LiveBrowser(browser_id="b1")

    async def go() -> None:
        await browser.acquire("A")
        started = asyncio.Event()

        async def fake_run() -> None:
            browser._agent_task = asyncio.current_task()
            try:
                started.set()
                await asyncio.sleep(100)
            finally:
                # Mirror run_agent's CAS-guarded finally: a no-op once the human took over.
                await browser.release("A")

        run = asyncio.create_task(fake_run())
        await started.wait()
        await asyncio.wait_for(browser.take_control(), timeout=2.0)  # must not hang
        await asyncio.sleep(0.05)
        assert run.cancelled()
        assert browser._state_tuple() == ("human", None, True)

    asyncio.run(go())


def test_monitor_and_wait_hands_off_in_fifo_order() -> None:
    browser = bsession.LiveBrowser(browser_id="b2")
    order: list[tuple[str, str]] = []

    async def go() -> None:
        await browser.acquire("A", "Alice")

        async def waiter(name: str) -> None:
            order.append((name, await browser.acquire(name, name, wait=True, max_wait=5)))

        task_b = asyncio.create_task(waiter("B"))
        await asyncio.sleep(0.05)
        task_c = asyncio.create_task(waiter("C"))
        await asyncio.sleep(0.05)
        assert [w.agent_id for w in browser._wait_queue] == ["B", "C"]
        await browser.release("A")  # hands to B (first in line)
        await asyncio.sleep(0.05)
        assert browser._state_tuple() == ("agent", "B", False)
        await browser.release("B")  # hands to C
        await task_b
        await task_c
        assert browser._state_tuple() == ("agent", "C", False)
        assert order == [("B", "acquired"), ("C", "acquired")]

    asyncio.run(go())


def test_wait_times_out_and_dequeues() -> None:
    browser = bsession.LiveBrowser(browser_id="b3")

    async def go() -> None:
        await browser.acquire("A")
        assert await browser.acquire("Z", "Z", wait=True, max_wait=0.2) == "timed_out"
        assert browser._wait_queue == []  # a timed-out waiter removes itself

    asyncio.run(go())


def test_take_control_evicts_waiters() -> None:
    browser = bsession.LiveBrowser(browser_id="b4")

    async def go() -> None:
        await browser.acquire("A")

        async def waiter() -> str:
            return await browser.acquire("W", "W", wait=True, max_wait=5)

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.05)
        await browser.take_control()  # preempt + pin -> waiters are evicted
        assert await task == "busy_human"
        assert browser._wait_queue == []

    asyncio.run(go())


# --- manager: ids + cap ------------------------------------------------------


def test_crashed_browser_reports_crashed_to_agent_and_viewer() -> None:
    # When Chromium dies, the browser reports "crashed" to the agent's next command
    # (it doesn't try to drive a corpse), surfaces it to viewers, and shows in describe().
    browser = bsession.LiveBrowser(browser_id="b3")

    async def go() -> None:
        browser._on_disconnected(None)  # simulate Playwright's disconnected event
        assert browser._crashed is True
        # An agent command short-circuits to a clear "crashed" status (no acquire).
        result = await browser.act_state("A", "Alice")
        assert result["ok"] is False and result["status"] == "crashed"
        # And it's reported in the fleet snapshot, with no tabs.
        desc = await browser.describe()
        assert desc["crashed"] is True and desc["tabs"] == []

    asyncio.run(go())


def test_intentional_close_is_not_reported_as_a_crash() -> None:
    # close() tears down the observer (which also fires `disconnected`); that's
    # expected teardown, not a crash, so _crashed must stay False.
    browser = bsession.LiveBrowser(browser_id="b3")

    async def go() -> None:
        browser._closed = True  # close() sets this before tearing down the observer
        browser._on_disconnected(None)
        assert browser._crashed is False

    asyncio.run(go())


def test_crashed_browsers_do_not_count_toward_the_fleet_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    # A crashed shell lingers (to report "crashed") but must not block opening a new
    # browser, so the cap counts only live browsers.
    monkeypatch.setattr(bsession, "_MAX_SESSIONS", 2)
    mgr = bsession.BrowserSessionManager()
    live = bsession.LiveBrowser(browser_id="alex-smith")
    dead = bsession.LiveBrowser(browser_id="riley-jones")
    dead._crashed = True
    mgr._browsers["alex-smith"] = live
    mgr._browsers["riley-jones"] = dead

    async def go() -> None:
        # 1 live + 1 crashed, cap 2 -> a new browser is still allowed (the crash is
        # not counted). Stub the launch to avoid starting real Chromium.
        async def fake_start_and_register(
            self: bsession.BrowserSessionManager, name: str, restore_tabs: Any = None, active_tab: int = 0
        ) -> object:
            obj = bsession.LiveBrowser(browser_id=name)
            self._browsers[name] = obj
            return obj

        monkeypatch.setattr(
            bsession.BrowserSessionManager, "_start_and_register_locked", fake_start_and_register
        )
        result = await mgr.create("morgan-lee")
        assert result.browser_id == "morgan-lee"  # allowed despite 2 entries (one crashed)
        assert mgr.has_browser("morgan-lee")

    asyncio.run(go())


def test_create_rejects_when_fleet_full(monkeypatch: pytest.MonkeyPatch) -> None:
    # The cap must reject before launching Chromium, so a small compute can't be OOM-ed.
    monkeypatch.setattr(bsession, "_MAX_SESSIONS", 3)
    mgr = bsession.BrowserSessionManager()
    mgr._browsers["a-one"] = object()  # type: ignore[assignment]
    mgr._browsers["b-two"] = object()  # type: ignore[assignment]
    mgr._browsers["c-three"] = object()  # type: ignore[assignment]

    async def go() -> None:
        # The cap message surfaces the exact locked copy "3/3 browsers open -- close one first."
        with pytest.raises(bsession.FleetFullError, match=r"3/3 browsers open -- close one first\."):
            await mgr.create()

    asyncio.run(go())


def test_create_generates_unique_names_and_regenerates_on_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two create()s with no name yield two DISTINCT registered names; a generator that
    # returns a duplicate first is retried (regenerate-on-collision under the lock).
    async def fake_start(
        self: bsession.LiveBrowser, _playwright: Any, restore_tabs: list[str] | None = None, active_tab: int = 0
    ) -> None:
        return None

    monkeypatch.setattr(bsession.LiveBrowser, "start", fake_start)
    mgr = bsession.BrowserSessionManager()
    mgr._playwright = object()  # type: ignore[assignment]  # skip async_playwright().start()

    # Inject a deterministic generator: returns "alex-smith", then "alex-smith" AGAIN
    # (a collision the manager must reject), then "riley-jones".
    scripted = iter(["alex-smith", "alex-smith", "riley-jones"])
    monkeypatch.setattr(bsession, "generate_browser_name", lambda: next(scripted))

    async def go() -> None:
        first = await mgr.create()
        second = await mgr.create()
        assert {first.browser_id, second.browser_id} == {"alex-smith", "riley-jones"}
        assert set(mgr._browsers) == {"alex-smith", "riley-jones"}

    asyncio.run(go())


def test_create_rejects_invalid_and_duplicate_user_names(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_start(
        self: bsession.LiveBrowser, _playwright: Any, restore_tabs: list[str] | None = None, active_tab: int = 0
    ) -> None:
        return None

    monkeypatch.setattr(bsession.LiveBrowser, "start", fake_start)
    mgr = bsession.BrowserSessionManager()
    mgr._playwright = object()  # type: ignore[assignment]

    async def go() -> None:
        created = await mgr.create("alex-smith")
        assert created.browser_id == "alex-smith"
        # A second create with the same typed name is rejected (409 at the HTTP layer).
        with pytest.raises(bsession.DuplicateBrowserNameError, match="already in use"):
            await mgr.create("alex-smith")
        # A syntactically invalid name is rejected (400 at the HTTP layer).
        with pytest.raises(bsession.InvalidBrowserNameError):
            await mgr.create("Bad Name")
        # A closed name frees up: re-creating it succeeds.
        await mgr.close("alex-smith")
        assert (await mgr.create("alex-smith")).browser_id == "alex-smith"

    asyncio.run(go())


def test_names_are_never_reused_after_close(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_start(
        self: bsession.LiveBrowser, _playwright: Any, restore_tabs: list[str] | None = None, active_tab: int = 0
    ) -> None:
        return None

    monkeypatch.setattr(bsession.LiveBrowser, "start", fake_start)
    mgr = bsession.BrowserSessionManager()
    mgr._playwright = object()  # type: ignore[assignment]

    async def go() -> None:
        a = await mgr.create("alex-smith")
        assert a.browser_id == "alex-smith"
        await mgr.close("alex-smith")
        # The closed name is gone -- a command on it would 404.
        with pytest.raises(KeyError):
            mgr.get("alex-smith")

    asyncio.run(go())


def test_profile_dir_round_trips_the_name() -> None:
    # The load-bearing prefix is preserved and the suffix is exactly the name.
    path = bsession._profile_dir("alex-smith")
    assert path.name == "browser-use-user-data-dir-alex-smith"
    assert "browser-use-user-data-dir-" in path.name


# --- persistence: restore + manifest (stubbed Chromium) ----------------------
# The autouse conftest fixture redirects the profile root + manifest path to tmp.


def _stub_start(monkeypatch: pytest.MonkeyPatch, fail_names: set[str] | None = None) -> list[tuple[str, Any]]:
    """Replace LiveBrowser.start with a no-op that records (name, restore_tabs); names in
    ``fail_names`` raise BrowserStartupError (to test resilient restore)."""
    calls: list[tuple[str, Any]] = []

    async def fake_start(
        self: bsession.LiveBrowser, _playwright: Any, restore_tabs: list[str] | None = None, active_tab: int = 0
    ) -> None:
        calls.append((self.browser_id, restore_tabs))
        if fail_names and self.browser_id in fail_names:
            raise bsession.BrowserStartupError(f"boom {self.browser_id}")

    monkeypatch.setattr(bsession.LiveBrowser, "start", fake_start)
    return calls


def _manager() -> bsession.BrowserSessionManager:
    mgr = bsession.BrowserSessionManager()
    mgr._playwright = object()  # type: ignore[assignment]  # skip async_playwright().start()
    return mgr


def test_restore_relaunches_saved_browsers_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_start(monkeypatch)
    manifest.write_manifest(
        manifest.Manifest(browsers=[manifest.ManifestEntry(id="alex-smith", tabs=["https://x"])])
    )
    mgr = _manager()
    asyncio.run(mgr.restore())
    assert mgr.has_browser("alex-smith")  # restored by name


def test_restore_passes_saved_tabs_and_comes_up_resting(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_start(monkeypatch)
    manifest.write_manifest(
        manifest.Manifest(browsers=[manifest.ManifestEntry(id="riley-jones", tabs=["https://x", "https://y"])])
    )
    mgr = _manager()
    asyncio.run(mgr.restore())
    assert ("riley-jones", ["https://x", "https://y"]) in calls  # saved tabs forwarded to start()
    restored = mgr.get("riley-jones")
    # Ownership/queues are NOT persisted: a restored browser is resting.
    assert restored._state_tuple() == ("human", None, False)
    assert restored._resume_queue == [] and restored._wait_queue == []


def test_snapshot_excludes_crashed_and_persists_only_topology() -> None:
    mgr = bsession.BrowserSessionManager()
    healthy = bsession.LiveBrowser(browser_id="alex-smith")
    healthy.controller = "agent"  # ownership state that must NOT be persisted
    healthy.owner_agent_id = "x"
    healthy.human_pinned = True
    crashed = bsession.LiveBrowser(browser_id="riley-jones")
    crashed._crashed = True
    mgr._browsers["alex-smith"] = healthy
    mgr._browsers["riley-jones"] = crashed

    async def go() -> bsession.fleet_manifest.Manifest:
        async with mgr._lock:
            return mgr._snapshot_manifest_locked()

    snap = asyncio.run(go())
    assert [e.id for e in snap.browsers] == ["alex-smith"]  # crashed browser excluded
    assert set(snap.browsers[0].model_dump().keys()) == {"id", "tabs", "active_tab"}


def test_fresh_workspace_restores_to_an_empty_fleet(monkeypatch: pytest.MonkeyPatch) -> None:
    # No manifest, no profiles on disk -> NO default browser, an EMPTY fleet. Nothing
    # is launched (no browser-0 seed); the first create() opens a browser later.
    calls = _stub_start(monkeypatch)
    mgr = _manager()
    asyncio.run(mgr.restore())
    assert calls == []  # nothing launched on a fresh workspace
    assert mgr._browsers == {}


def test_manifest_loss_with_surviving_profiles_relaunches_them(monkeypatch: pytest.MonkeyPatch) -> None:
    # No manifest, but a name-valid profile dir survived on the volume -> relaunch it
    # (tabs unknown), rather than treating this as a first boot and wiping the saved login.
    (bsession._PROFILE_ROOT / "browser-use-user-data-dir-alex-smith").mkdir(parents=True)
    calls = _stub_start(monkeypatch)
    mgr = _manager()
    asyncio.run(mgr.restore())
    assert ("alex-smith", None) in calls and mgr.has_browser("alex-smith")


def test_legacy_numeric_profile_dirs_are_not_resurrected(monkeypatch: pytest.MonkeyPatch) -> None:
    # An upgraded workspace may have old numeric profile dirs (browser-use-user-data-dir-0).
    # is_valid_browser_name rejects pure-numeric suffixes, so they are NOT relaunched as
    # bogus "0" named browsers -- they fall through to the orphan sweep instead.
    root = bsession._PROFILE_ROOT
    (root / "browser-use-user-data-dir-0").mkdir(parents=True)
    (root / "browser-use-user-data-dir-2").mkdir(parents=True)
    calls = _stub_start(monkeypatch)
    mgr = _manager()
    asyncio.run(mgr.restore())
    assert calls == []  # no numeric dir relaunched
    assert mgr._browsers == {}
    # And they are swept (not kept around forever as stale numeric profiles).
    assert not (root / "browser-use-user-data-dir-0").exists()
    assert not (root / "browser-use-user-data-dir-2").exists()


def test_restore_keeps_a_flaked_browser_for_next_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    # A transient relaunch failure must NOT lose the saved browser: it stays in the
    # manifest (for a next-boot retry) and its profile is NOT swept. (Durability.)
    (bsession._PROFILE_ROOT / "browser-use-user-data-dir-riley-jones").mkdir(parents=True)
    _stub_start(monkeypatch, fail_names={"riley-jones"})
    manifest.write_manifest(
        manifest.Manifest(
            browsers=[
                manifest.ManifestEntry(id="alex-smith"),
                manifest.ManifestEntry(id="riley-jones", tabs=["https://x"]),
                manifest.ManifestEntry(id="morgan-lee"),
            ],
        )
    )
    mgr = _manager()
    asyncio.run(mgr.restore())
    assert mgr.has_browser("alex-smith") and mgr.has_browser("morgan-lee")
    assert not mgr.has_browser("riley-jones")  # flaked, not live
    reconciled = manifest.read_manifest()
    assert reconciled is not None
    entry = next((e for e in reconciled.browsers if e.id == "riley-jones"), None)
    assert entry is not None and entry.tabs == ["https://x"]  # preserved for retry
    assert (bsession._PROFILE_ROOT / "browser-use-user-data-dir-riley-jones").exists()  # NOT deleted


def test_restore_sweeps_orphan_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    root = bsession._PROFILE_ROOT
    for name in ("alex-smith", "riley-jones", "orphan-gone"):
        (root / f"browser-use-user-data-dir-{name}").mkdir(parents=True)
    _stub_start(monkeypatch)
    manifest.write_manifest(
        manifest.Manifest(
            browsers=[manifest.ManifestEntry(id="alex-smith"), manifest.ManifestEntry(id="riley-jones")]
        )
    )
    mgr = _manager()
    asyncio.run(mgr.restore())
    assert not (root / "browser-use-user-data-dir-orphan-gone").exists()  # orphan swept
    assert (root / "browser-use-user-data-dir-alex-smith").exists()
    assert (root / "browser-use-user-data-dir-riley-jones").exists()


def test_state_on_busy_browser_does_not_enqueue_the_agent() -> None:
    # A passive `state` peek at a browser another agent holds must NOT enrol the
    # caller as a waiter (only state-changing commands queue for resume).
    browser = bsession.LiveBrowser(browser_id="b0")

    async def go() -> None:
        await browser.acquire("A", "Alice")  # agent A holds it
        result = await browser.act_state("B", "Bob")  # B just looks
        assert result["ok"] is False and result["status"] == "busy_agent"
        assert browser._waiting_names() == []  # B was NOT queued

    asyncio.run(go())


# --- direct control: sticky lease + per-command CAS --------------------------


def _direct_ready(name: str = "alex-smith") -> bsession.LiveBrowser:
    # A LiveBrowser wired enough to run run_action without a real Chromium: a
    # non-None _context passes the "closed" guard, and a pre-set _action_handler
    # skips constructing a real ActionHandler (the fake action ignores it).
    browser = bsession.LiveBrowser(browser_id=name)
    browser._context = object()  # type: ignore[assignment]
    browser._action_handler = object()  # type: ignore[assignment]
    return browser


def test_run_action_acquires_then_reports_busy_to_others() -> None:
    browser = _direct_ready()

    async def fake(_handler: Any) -> dict[str, Any]:
        return {"did": "it"}

    async def go() -> None:
        # First command acquires the sticky lease and returns the owner snapshot.
        result = await browser.run_action("A", "Alice", fake)
        assert result["ok"] and result["did"] == "it"
        assert result["controller"] == "agent" and result["owner_agent_id"] == "A"
        assert browser._state_tuple() == ("agent", "A", False)
        # Another agent's command is refused (agents never preempt).
        result = await browser.run_action("B", "Bob", fake)
        assert result["ok"] is False and result["status"] == "busy_agent"
        # A human take-control blocks the owning agent's next command too.
        await browser.take_control()
        result = await browser.run_action("A", "Alice", fake)
        assert result["ok"] is False and result["status"] == "busy_human"

    asyncio.run(go())


def test_run_action_per_command_cas_catches_mid_sequence_takeover(monkeypatch: pytest.MonkeyPatch) -> None:
    # The critical guard: even if acquire reports success, the per-command CAS
    # re-checks ownership right before acting -- so a take-control that landed in
    # between makes the command a clean no-op instead of touching the human's browser.
    browser = _direct_ready("riley-jones")

    async def fake_acquire(*_args: Any, **_kwargs: Any) -> str:
        return "acquired"  # pretend we got it, but DON'T flip control state

    monkeypatch.setattr(bsession.LiveBrowser, "acquire", fake_acquire)

    async def fake(_handler: Any) -> dict[str, Any]:
        raise AssertionError("the action must NOT run when control was lost")

    async def go() -> None:
        result = await browser.run_action("A", "Alice", fake)
        assert result["ok"] is False and result["status"] == "lost_control"

    asyncio.run(go())


def test_idle_lease_sweep_releases_only_a_quiet_lease() -> None:
    browser = _direct_ready("morgan-lee")

    async def go() -> None:
        await browser.acquire("A", "Alice")
        # Fresh lease -> not swept.
        assert await browser._sweep_idle_lease() is False
        assert browser._state_tuple() == ("agent", "A", False)
        # A running task is connection-bound -> exempt even if "idle".
        browser._lease_touched_at = time.monotonic() - (bsession._LEASE_IDLE_TTL + 10)
        browser._agent_task = asyncio.current_task()
        assert await browser._sweep_idle_lease() is False
        # A quiet, task-free lease past the TTL -> released back to the human.
        browser._agent_task = None
        assert await browser._sweep_idle_lease() is True
        assert browser._state_tuple() == ("human", None, False)

    asyncio.run(go())


# --- cast fan-out: outbound queue per socket (the Flask<->loop WS inversion) ---


def test_register_cast_queue_seeds_initial_control_and_tabs() -> None:
    # A freshly-registered cast queue is seeded with the current control + tabs sync
    # BEFORE any live frame, so the viewer's first messages are deterministic.
    browser = bsession.LiveBrowser(browser_id="b1")
    browser._context = None  # _tab_list returns [] with no context

    async def go() -> None:
        q = await browser.register_cast_queue()
        first = _pop_json(q)
        second = _pop_json(q)
        assert first["type"] == "control" and first["owner"] == "human"
        assert second["type"] == "tabs" and second["tabs"] == []
        assert q.empty()  # not crashed -> no crash message
        assert q in browser._cast_queues

    asyncio.run(go())


def test_broadcast_fans_out_to_registered_queues_and_unregister_removes() -> None:
    browser = bsession.LiveBrowser(browser_id="b1")
    browser._context = None

    async def go() -> None:
        q = await browser.register_cast_queue()
        # Drain the initial seed so we only see the broadcast below.
        while not q.empty():
            q.get_nowait()
        browser._broadcast({"type": "frame", "data": "abc"})
        msg = _pop_json(q)
        assert msg == {"type": "frame", "data": "abc"}
        # Unregister stops further fan-out to this queue.
        await browser.unregister_cast_queue(q)
        assert q not in browser._cast_queues
        browser._broadcast({"type": "frame", "data": "def"})
        assert q.empty()

    asyncio.run(go())


def test_broadcast_drops_oldest_frame_when_a_slow_client_queue_is_full(monkeypatch: pytest.MonkeyPatch) -> None:
    # A client that falls behind must not block the loop: _broadcast drops the OLDEST
    # buffered frame and enqueues the newest (only the latest frame matters).
    monkeypatch.setattr(bsession, "_CAST_QUEUE_MAX_SIZE", 2)
    browser = bsession.LiveBrowser(browser_id="b1")
    browser._context = None

    async def go() -> None:
        q = await browser.register_cast_queue()
        while not q.empty():
            q.get_nowait()
        for n in range(5):
            browser._broadcast({"type": "frame", "data": str(n)})
        # maxsize 2 -> only the two most-recent frames survive (3 and 4).
        survivors = []
        while not q.empty():
            survivors.append(_pop_json(q)["data"])
        assert survivors == ["3", "4"]

    asyncio.run(go())
