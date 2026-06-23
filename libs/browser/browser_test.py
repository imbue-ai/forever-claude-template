import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from browser import session as bsession


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
    browser = bsession.LiveBrowser(browser_id=1)

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
    browser = bsession.LiveBrowser(browser_id=1)
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
    browser = bsession.LiveBrowser(browser_id=1)

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


def test_return_to_agents_only_unpins_a_pinned_human() -> None:
    browser = bsession.LiveBrowser(browser_id=1)

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
    browser = bsession.LiveBrowser(browser_id=1)

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
    browser = bsession.LiveBrowser(browser_id=2)
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
    browser = bsession.LiveBrowser(browser_id=3)

    async def go() -> None:
        await browser.acquire("A")
        assert await browser.acquire("Z", "Z", wait=True, max_wait=0.2) == "timed_out"
        assert browser._wait_queue == []  # a timed-out waiter removes itself

    asyncio.run(go())


def test_take_control_evicts_waiters() -> None:
    browser = bsession.LiveBrowser(browser_id=4)

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


def test_create_rejects_when_fleet_full(monkeypatch: pytest.MonkeyPatch) -> None:
    # The cap must reject before launching Chromium, so a small compute can't be OOM-ed.
    monkeypatch.setattr(bsession, "_MAX_SESSIONS", 2)
    mgr = bsession.BrowserSessionManager()
    mgr._browsers[1] = object()  # type: ignore[assignment]
    mgr._browsers[2] = object()  # type: ignore[assignment]

    async def go() -> None:
        with pytest.raises(bsession.FleetFullError, match="Too many open browsers"):
            await mgr.create()

    asyncio.run(go())


def test_ids_are_monotonic_and_never_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    # Stub the Chromium launch so we exercise pure id-allocation logic.
    async def fake_start(self: bsession.LiveBrowser, _playwright: Any) -> None:
        return None

    monkeypatch.setattr(bsession.LiveBrowser, "start", fake_start)
    mgr = bsession.BrowserSessionManager()
    mgr._playwright = object()  # type: ignore[assignment]  # skip async_playwright().start()

    async def go() -> None:
        assert (await mgr.ensure_browser_0()).browser_id == 0
        assert (await mgr.ensure_browser_0()).browser_id == 0  # idempotent
        assert (await mgr.create()).browser_id == 1
        assert (await mgr.create()).browser_id == 2
        await mgr.close(1)
        # id 1 is gone for good -- the next create gets 3, and `task 1` would 404.
        assert (await mgr.create()).browser_id == 3
        with pytest.raises(KeyError):
            mgr.get(1)

    asyncio.run(go())


# --- direct control: sticky lease + per-command CAS --------------------------


def _direct_ready(browser_id: int = 1) -> bsession.LiveBrowser:
    # A LiveBrowser wired enough to run run_action without a real Chromium: a
    # non-None _context passes the "closed" guard, and a pre-set _action_handler
    # skips constructing a real ActionHandler (the fake action ignores it).
    browser = bsession.LiveBrowser(browser_id=browser_id)
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
    browser = _direct_ready(2)

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
    browser = _direct_ready(3)

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
