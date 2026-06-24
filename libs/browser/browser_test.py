import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from browser import manifest
from browser import session as bsession


async def _noop_wake(self: bsession.LiveBrowser, agent_id: str, agent_name: str | None) -> None:
    """Stand-in for ``_wake_agent`` in tests: skip the real ``mngr message`` subprocess."""


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


def test_enqueue_on_busy_queues_for_resume_and_wakes_on_handback(monkeypatch: pytest.MonkeyPatch) -> None:
    # Direct-control handoff: a human takes control, the agent's next command is
    # rejected (busy_human) and the agent is queued to resume. When the human hands
    # back, the queued agent is granted control and messaged to resume.
    woken: list[str | None] = []

    async def fake_wake(self: bsession.LiveBrowser, agent_id: str, agent_name: str | None) -> None:
        woken.append(agent_name)

    monkeypatch.setattr(bsession.LiveBrowser, "_wake_agent", fake_wake)
    browser = bsession.LiveBrowser(browser_id=1)

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
    browser = bsession.LiveBrowser(browser_id=1)

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


def test_stale_human_pin_yields_to_a_queued_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    # A human who took control but walked away (no input within the active grace)
    # should not block a queued agent forever: the sweep hands the browser back.
    monkeypatch.setattr(bsession.LiveBrowser, "_wake_agent", _noop_wake)
    browser = bsession.LiveBrowser(browser_id=1)

    async def go() -> None:
        await browser.acquire("A", "Alice")
        await browser.take_control()  # pin is fresh -> A is blocked and queues
        assert await browser.acquire("A", "Alice", wait=False, enqueue_on_busy=True) == "busy_human"
        assert await browser._sweep_stale_human_pin() is False  # pin still active: not yet
        # The human goes quiet (no input past the grace) -> the pin is now stale.
        browser._human_touched_at = time.monotonic() - bsession._HUMAN_ACTIVE_GRACE - 1
        assert await browser._sweep_stale_human_pin() is True
        assert browser._state_tuple() == ("agent", "A", False)

    asyncio.run(go())


def test_stale_human_pin_persists_when_nobody_waits() -> None:
    # With no agent queued, a stale pin is NOT swept away (there's nobody to hand to);
    # the next agent that wants it takes it lazily via acquire().
    browser = bsession.LiveBrowser(browser_id=1)

    async def go() -> None:
        await browser.acquire("A", "Alice")
        await browser.take_control()
        browser._human_touched_at = time.monotonic() - bsession._HUMAN_ACTIVE_GRACE - 1  # stale
        assert await browser._sweep_stale_human_pin() is False  # nobody waiting -> persists
        assert browser._state_tuple() == ("human", None, True)
        # A newly-arriving agent finds the pin stale and just takes it.
        assert await browser.acquire("B", "Bob", wait=False) == "acquired"

    asyncio.run(go())


def test_unclaimed_grant_passes_to_next_waiter(monkeypatch: pytest.MonkeyPatch) -> None:
    # An agent granted the browser from the resume queue but that never sends a
    # command (interrupted/killed) has its grant revoked after the claim window, so
    # the browser doesn't sit idle on a no-show.
    monkeypatch.setattr(bsession.LiveBrowser, "_wake_agent", _noop_wake)
    browser = bsession.LiveBrowser(browser_id=1)

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


def test_crashed_browser_reports_crashed_to_agent_and_viewer() -> None:
    # When Chromium dies, the browser reports "crashed" to the agent's next command
    # (it doesn't try to drive a corpse), surfaces it to viewers, and shows in describe().
    browser = bsession.LiveBrowser(browser_id=3)

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
    browser = bsession.LiveBrowser(browser_id=3)

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
    live = bsession.LiveBrowser(browser_id=1)
    dead = bsession.LiveBrowser(browser_id=2)
    dead._crashed = True
    mgr._browsers[1] = live
    mgr._browsers[2] = dead
    mgr._next_id = 3  # ids 1 and 2 were already handed out

    async def go() -> None:
        # 1 live + 1 crashed, cap 2 -> a new browser is still allowed (the crash is
        # not counted). We can't launch real Chromium here, so just assert the cap
        # check passes by confirming it does NOT raise FleetFullError before launch.
        # Stub the launch to avoid starting Chromium.
        async def fake_start_and_register(self: bsession.BrowserSessionManager, browser_id: int) -> object:
            obj = bsession.LiveBrowser(browser_id=browser_id)
            self._browsers[browser_id] = obj
            return obj

        monkeypatch.setattr(
            bsession.BrowserSessionManager, "_start_and_register_locked", fake_start_and_register
        )
        result = await mgr.create()
        assert result.browser_id == 3  # allowed despite 2 entries (one crashed)

    asyncio.run(go())


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
    async def fake_start(
        self: bsession.LiveBrowser, _playwright: Any, restore_tabs: list[str] | None = None
    ) -> None:
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


# --- persistence: restore + manifest (stubbed Chromium) ----------------------
# The autouse conftest fixture redirects the profile root + manifest path to tmp.


def _stub_start(monkeypatch: pytest.MonkeyPatch, fail_ids: set[int] | None = None) -> list[tuple[int, Any]]:
    """Replace LiveBrowser.start with a no-op that records (id, restore_tabs); ids in
    ``fail_ids`` raise BrowserStartupError (to test resilient restore)."""
    calls: list[tuple[int, Any]] = []

    async def fake_start(
        self: bsession.LiveBrowser, _playwright: Any, restore_tabs: list[str] | None = None
    ) -> None:
        calls.append((self.browser_id, restore_tabs))
        if fail_ids and self.browser_id in fail_ids:
            raise bsession.BrowserStartupError(f"boom {self.browser_id}")

    monkeypatch.setattr(bsession.LiveBrowser, "start", fake_start)
    return calls


def _manager() -> bsession.BrowserSessionManager:
    mgr = bsession.BrowserSessionManager()
    mgr._playwright = object()  # type: ignore[assignment]  # skip async_playwright().start()
    return mgr


def test_restore_sets_next_id_high_water_mark(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_start(monkeypatch)
    # next_id is stale (1) but a browser with id 5 exists -> next_id must jump past it,
    # so a retired id is never re-handed-out.
    manifest.write_manifest(
        manifest.Manifest(next_id=1, browsers=[manifest.ManifestEntry(id=5, tabs=["https://x"])])
    )
    mgr = _manager()
    asyncio.run(mgr.restore())
    assert mgr._next_id == 6
    assert mgr.has_browser(5) and mgr.has_browser(0)  # restored 5, seeded the default 0


def test_restore_passes_saved_tabs_and_comes_up_resting(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_start(monkeypatch)
    manifest.write_manifest(
        manifest.Manifest(next_id=3, browsers=[manifest.ManifestEntry(id=2, tabs=["https://x", "https://y"])])
    )
    mgr = _manager()
    asyncio.run(mgr.restore())
    assert (2, ["https://x", "https://y"]) in calls  # saved tabs forwarded to start()
    restored = mgr.get(2)
    # Ownership/queues are NOT persisted: a restored browser is resting.
    assert restored._state_tuple() == ("human", None, False)
    assert restored._resume_queue == [] and restored._wait_queue == []


def test_snapshot_excludes_crashed_and_persists_only_topology() -> None:
    mgr = bsession.BrowserSessionManager()
    healthy = bsession.LiveBrowser(browser_id=0)
    healthy.controller = "agent"  # ownership state that must NOT be persisted
    healthy.owner_agent_id = "x"
    healthy.human_pinned = True
    crashed = bsession.LiveBrowser(browser_id=1)
    crashed._crashed = True
    mgr._browsers[0] = healthy
    mgr._browsers[1] = crashed

    async def go() -> bsession.fleet_manifest.Manifest:
        async with mgr._lock:
            return await mgr._snapshot_manifest_locked()

    snap = asyncio.run(go())
    assert [e.id for e in snap.browsers] == [0]  # crashed browser 1 excluded
    assert set(snap.browsers[0].model_dump().keys()) == {"id", "tabs", "active_tab"}


def test_first_boot_seeds_browser_0_at_home(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_start(monkeypatch)  # no manifest, no profiles on disk
    mgr = _manager()
    asyncio.run(mgr.restore())
    assert calls == [(0, None)]  # only the default browser, at the home page
    assert manifest.read_manifest() is not None  # manifest written so the systems agree


def test_manifest_loss_with_surviving_profiles_relaunches_them(monkeypatch: pytest.MonkeyPatch) -> None:
    # No manifest, but a profile dir survived on the volume -> relaunch it (tabs unknown),
    # rather than treating this as a first boot and wiping the saved login.
    (bsession._PROFILE_ROOT / "browser-use-user-data-dir-2").mkdir(parents=True)
    calls = _stub_start(monkeypatch)
    mgr = _manager()
    asyncio.run(mgr.restore())
    assert (2, None) in calls and mgr.has_browser(2)
    assert mgr.has_browser(0)


def test_restore_skips_a_failing_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_start(monkeypatch, fail_ids={2})
    manifest.write_manifest(
        manifest.Manifest(
            next_id=4,
            browsers=[manifest.ManifestEntry(id=0), manifest.ManifestEntry(id=2), manifest.ManifestEntry(id=3)],
        )
    )
    mgr = _manager()
    asyncio.run(mgr.restore())
    assert mgr.has_browser(0) and mgr.has_browser(3) and not mgr.has_browser(2)
    reconciled = manifest.read_manifest()
    assert reconciled is not None and all(e.id != 2 for e in reconciled.browsers)


def test_restore_sweeps_orphan_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    root = bsession._PROFILE_ROOT
    for n in (0, 2, 9):
        (root / f"browser-use-user-data-dir-{n}").mkdir(parents=True)
    _stub_start(monkeypatch)
    manifest.write_manifest(
        manifest.Manifest(next_id=3, browsers=[manifest.ManifestEntry(id=0), manifest.ManifestEntry(id=2)])
    )
    mgr = _manager()
    asyncio.run(mgr.restore())
    assert not (root / "browser-use-user-data-dir-9").exists()  # orphan (no live browser) swept
    assert (root / "browser-use-user-data-dir-0").exists() and (root / "browser-use-user-data-dir-2").exists()


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
