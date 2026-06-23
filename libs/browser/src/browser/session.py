"""Live browser fleet: headless Chromium + CDP screencast + a per-browser ownership state machine.

Each :class:`LiveBrowser` owns one headless Chromium (launched and driven by
``browser_use.BrowserSession``) plus a second, observer-only Playwright
connection over the same CDP endpoint. The Playwright side does the things
browser-use does not: stream the live view to the user (CDP
``Page.startScreencast`` -> base64 JPEG frames over a WebSocket) and inject the
user's mouse/keyboard (CDP ``Input.dispatch*Event``). The browser-use side does
the AI driving (``Agent.run``). Both clients share the one Chromium, so the
human sees exactly what the agent does and vice versa.

Ownership is a small per-browser state machine, and it is the heart of this
module. Many agents (a chat agent plus its sub-agents, each a distinct
``MNGR_AGENT_ID``) share one fleet; any single browser is controlled by exactly
one party at a time: a specific agent, or the human. Every control change goes
through the single writer :meth:`LiveBrowser._write_control_locked`, called only
under ``_control_lock`` with a compare-and-set guard, so there is no bespoke
ordering anywhere and "single asyncio process" actually means atomic. The state:

* ``controller`` -- ``"human"`` or ``"agent"``.
* ``owner_agent_id`` -- which agent holds it (when ``controller == "agent"``).
* ``human_pinned`` -- the human explicitly took control; agents are locked out
  until the human hands back. (Idle ``human`` with ``human_pinned`` false is
  the resting state: human-drivable and agent-acquirable.)

Rules that fall out of this and never need a special case:

* Agents NEVER preempt anyone. :meth:`acquire` succeeds only against an unpinned
  human (or the same agent re-acquiring); against another agent it parks the
  caller in a FIFO wait-queue (monitor-and-wait) until that agent releases.
* The human ALWAYS wins: :meth:`take_control` preempts whatever agent is driving
  (cancelling its run) and pins; a pinned browser evicts any waiters.
* Ownership is bound to the live ``task`` request (see runner.py): if the agent
  process dies, the request disconnects, the run is cancelled, and the browser
  is released -- no fire-and-forget locks, no stuck owners.

The Anthropic API key is read lazily from the environment (and a fresh re-read of
``$MNGR_HOST_DIR/env``) at run time, so a key submitted after this service booted
is still picked up without a restart. Direct Anthropic API key only -- the Imbue
Cloud / litellm proxy (``ANTHROPIC_BASE_URL``) path is intentionally unsupported.
"""

import asyncio
import base64
import json
import os
import time
from collections.abc import Awaitable
from collections.abc import Callable
from pathlib import Path
from typing import Any
from typing import Literal

from browser_use import Agent
from browser_use import BrowserSession
from browser_use import ChatAnthropic
from browser_use.skill_cli.actions import ActionHandler
from fastapi import WebSocket
from loguru import logger
from playwright.async_api import Browser
from playwright.async_api import BrowserContext
from playwright.async_api import CDPSession
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import Playwright
from playwright.async_api import async_playwright
from pydantic import PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel

# browser-use phones home anonymized telemetry by default; disable it (the
# compute has no business making that call, and it spams connection-error logs
# where egress is restricted). setdefault so an explicit opt-in still wins.
os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")

# Errors expected when a page/target/CDP session goes away underneath us (tab
# closed, navigation, browser killed). PlaywrightError covers TargetClosedError.
_BROWSER_ERRORS = (RuntimeError, ConnectionError, OSError, PlaywrightError)

ControlOwner = Literal["human", "agent"]

# An event emitted by a running task: thinking / action / status / done / error /
# preempted. The runner streams these to the agent's CLI as line-delimited JSON.
TaskEvent = dict[str, Any]
EventSink = Callable[[TaskEvent], Awaitable[None]]

# JPEG screencast tuned so a single base64 JSON frame stays comfortably under the
# system_interface WebSocket proxy's 1 MiB per-message cap, even on busy pages.
_SCREENCAST_FORMAT = "jpeg"
_SCREENCAST_QUALITY = 55
_SCREENCAST_MAX_WIDTH = 1280
_SCREENCAST_MAX_HEIGHT = 800
# Every frame: the first frame after a tab switch arrives sooner, so clicking a
# tab feels snappier. Slightly more bandwidth than skipping frames.
_SCREENCAST_EVERY_NTH_FRAME = 1

# Deferred-install marker (see scripts/deferred_install.sh). Chromium installs
# asynchronously on first container boot; launching a browser before it exists
# fails, so callers gate on this. No Xvfb: CDP streaming/input are headless.
_PLAYWRIGHT_MARKER = Path("/var/lib/minds/deferred-install/done.playwright")

# Default model. browser-use's own default LLM is ChatBrowserUse (its hosted
# model), so to drive with the user's Anthropic key we pass ChatAnthropic
# explicitly. Overridable via env for easy iteration; the string is sent to the
# API as-is (browser-use accepts an arbitrary model string).
_DEFAULT_MODEL = os.environ.get("BROWSER_USE_MODEL", "claude-sonnet-4-6")

# Headless by default. CDP screencast + input are display-independent (they work
# in headless Chromium), so no Xvfb is needed. Set BROWSER_HEADLESS=0 to run
# headful (stronger anti-bot fidelity) if a site blocks headless.
_HEADLESS = os.environ.get("BROWSER_HEADLESS", "1") != "0"

# Page the browser opens on, and the default for "New tab".
_HOME_URL = os.environ.get("BROWSER_HOME_URL", "https://www.google.com")

# Server-side cast keepalive: a static page emits no screencast frames, so without
# traffic the system_interface WS proxy closes the idle stream (~30s). A periodic
# ping keeps the backend->client direction alive between real frames.
_KEEPALIVE_SECONDS = 10

# Each live session = a headless Chromium + a Playwright observer; cap the concurrent
# count so a small compute (e.g. 4 GB) can't be OOM-ed. Override via BROWSER_MAX_SESSIONS.
_MAX_SESSIONS = int(os.environ.get("BROWSER_MAX_SESSIONS", "5"))

# Hard ceilings on a single browser-use task so a hung or non-cancel-safe run can
# never pin a browser forever (the connection-disconnect path is the primary
# release; these are the backstop). Both env-tunable.
_TASK_MAX_STEPS = int(os.environ.get("BROWSER_TASK_MAX_STEPS", "100"))
_TASK_MAX_SECONDS = float(os.environ.get("BROWSER_TASK_MAX_SECONDS", "900"))

# Direct-control ownership is a STICKY LEASE: an agent acquires a browser on its
# first command and holds it across subsequent commands. Unlike a `task` (whose
# ownership is bound to the long run), a lease has no live connection to detect a
# dead/wandered-off owner, so it auto-releases after this many seconds with no
# command (the keepalive loop sweeps it). The human take-control is the instant
# escape hatch; this TTL is the backstop. Env-tunable.
_LEASE_IDLE_TTL = float(os.environ.get("BROWSER_LEASE_IDLE_TTL", "90"))

# Where `screenshot` writes PNGs (relative to the daemon's cwd = repo root). The
# CLI prints the path and the agent reads the file; agent + daemon share the FS.
_SCREENSHOT_DIR = Path(os.environ.get("BROWSER_SCREENSHOT_DIR", "runtime/browser-screenshots"))


def _action_summary(action: Any) -> str:
    """One-line label for a browser-use action dict (e.g. ``switch: {"tab_id": "230B"}``)."""
    if isinstance(action, dict):
        for key, value in action.items():
            if key == "interacted_element":
                continue
            if value is None or (isinstance(value, dict) and not value):
                return str(key)
            return f"{key}: {json.dumps(value, default=str)}"
    return str(action)[:80]


def _parse_env_file(text: str) -> dict[str, str]:
    """Parse a ``KEY=VALUE`` env file (the format claude_auth writes), tolerating quotes."""
    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1].replace('\\"', '"')
        if key:
            result[key] = value
    return result


def resolve_anthropic_key() -> str | None:
    """Return a direct Anthropic API key from the process env or ``$MNGR_HOST_DIR/env``.

    Anthropic API only: we deliberately do NOT read ``ANTHROPIC_BASE_URL`` / support
    the Imbue Cloud / litellm proxy path. The fallback re-reads the host env file fresh
    so a key submitted after this service started is still found without a restart.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        host_dir = os.environ.get("MNGR_HOST_DIR")
        if host_dir:
            env_path = Path(host_dir) / "env"
            if env_path.exists():
                api_key = _parse_env_file(env_path.read_text()).get("ANTHROPIC_API_KEY")
    return api_key


def anthropic_key_status() -> tuple[bool, str]:
    """Return ``(available, reason)`` for gating the "New browser" menu item."""
    if resolve_anthropic_key():
        return True, "Anthropic API key available"
    return (
        False,
        "Browser sessions need an Anthropic API key. Create the workspace with the "
        "'Anthropic API key' provider (the 'Claude subscription' option has no usable key).",
    )


def deferred_install_ready() -> tuple[bool, str]:
    """Return ``(ready, reason)`` once Chromium is installed."""
    if os.environ.get("BROWSER_SKIP_INSTALL_CHECK") == "1":
        return True, "ready"  # host/CI testing without the deferred-install marker
    if not _PLAYWRIGHT_MARKER.exists():
        return False, "Chromium is still installing in this workspace; try again in a minute."
    return True, "ready"


def _enabled_event() -> asyncio.Event:
    """An asyncio.Event that starts SET -- the resting controller is the human, so
    human input is enabled from construction (not only after :meth:`LiveBrowser.start`)."""
    event = asyncio.Event()
    event.set()
    return event


class BrowserStartupError(Exception):
    """Raised when a Chromium session fails to come up (e.g. no CDP endpoint)."""


class FleetFullError(BrowserStartupError):
    """Raised when the fleet is already at ``_MAX_SESSIONS`` (maps to HTTP 409)."""


class _AcquireWaiter:
    """One agent parked in a browser's FIFO wait-queue (monitor-and-wait).

    ``event`` is set when the waiter is resolved; ``granted`` distinguishes the two
    outcomes -- handed ownership (the prior agent released) vs evicted because a
    human took control (agents never wait on a human-pinned browser).
    """

    def __init__(self, agent_id: str, agent_name: str | None) -> None:
        self.agent_id = agent_id
        self.agent_name = agent_name
        self.event = asyncio.Event()
        self.granted = False


class LiveBrowser(MutableModel):
    """One headless Chromium streamed to the user, optionally driven by a browser-use agent."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    # The integer the user/agent sees (0 is the permanent default browser). Stable
    # and never reused: a closed id is gone, so a cached id is the same browser or a 404.
    browser_id: int
    controller: ControlOwner = "human"
    owner_agent_id: str | None = None
    owner_agent_name: str | None = None
    human_pinned: bool = False

    _playwright: Playwright = PrivateAttr()
    _bu_session: BrowserSession = PrivateAttr()
    _observer: Browser | None = PrivateAttr(default=None)
    _context: BrowserContext | None = PrivateAttr(default=None)
    _active_page: Page | None = PrivateAttr(default=None)
    _active_cdp: CDPSession | None = PrivateAttr(default=None)
    _agent: Agent | None = PrivateAttr(default=None)
    _agent_task: "asyncio.Task[None] | None" = PrivateAttr(default=None)
    _run_on_event: EventSink | None = PrivateAttr(default=None)
    _input_enabled: asyncio.Event = PrivateAttr(default_factory=_enabled_event)
    _cast_sockets: list[WebSocket] = PrivateAttr(default_factory=list)
    _latest_frame: str | None = PrivateAttr(default=None)
    _send_in_flight: bool = PrivateAttr(default=False)
    _nav_tracked: set[Page] = PrivateAttr(default_factory=set)
    _active_target_id: str | None = PrivateAttr(default=None)
    _keepalive_task: "asyncio.Task[None] | None" = PrivateAttr(default=None)
    _closed: bool = PrivateAttr(default=False)
    # Serializes screencast/active-tab changes (slow CDP work).
    _lock: asyncio.Lock = PrivateAttr(default_factory=asyncio.Lock)
    # Serializes ALL ownership changes -- the single mutual-exclusion primitive.
    _control_lock: asyncio.Lock = PrivateAttr(default_factory=asyncio.Lock)
    _wait_queue: list[_AcquireWaiter] = PrivateAttr(default_factory=list)
    # Direct-control: browser-use's own action executor (lazily bound to _bu_session),
    # the last `state`'s numbered elements (so `click <index>` resolves a node), and
    # the sticky-lease activity timestamp the idle-TTL sweep checks.
    _action_handler: ActionHandler | None = PrivateAttr(default=None)
    _selector_map: dict[int, Any] = PrivateAttr(default_factory=dict)
    _lease_touched_at: float = PrivateAttr(default=0.0)
    _screenshot_seq: int = PrivateAttr(default=0)

    async def start(self, playwright: Playwright) -> None:
        """Launch the headless Chromium (browser-use) and attach the Playwright observer."""
        self._playwright = playwright
        self._input_enabled.set()
        chromium_path = playwright.chromium.executable_path
        self._bu_session = BrowserSession(
            headless=_HEADLESS,
            executable_path=chromium_path,
            args=["--disable-dev-shm-usage"],
            keep_alive=True,
            # Pin a fixed viewport + window so every site renders at the same
            # resolution -- a consistent "Chromium in a small window", not a size
            # that shifts per page. Matches the screencast cap so frames never scale.
            viewport={"width": _SCREENCAST_MAX_WIDTH, "height": _SCREENCAST_MAX_HEIGHT},
            window_size={"width": _SCREENCAST_MAX_WIDTH, "height": _SCREENCAST_MAX_HEIGHT},
            device_scale_factor=1,
        )
        await self._bu_session.start()
        cdp_url = self._bu_session.cdp_url
        if not cdp_url:
            raise BrowserStartupError("browser-use BrowserSession did not expose a cdp_url after start")
        observer = await playwright.chromium.connect_over_cdp(cdp_url)
        self._observer = observer
        self._context = observer.contexts[0] if observer.contexts else await observer.new_context()
        self._context.on("page", self._on_new_page)
        pages = self._context.pages
        page = pages[0] if pages else await self._context.new_page()
        self._track_nav(page)
        await self._set_active_page(page)
        try:
            await page.goto(_HOME_URL)
        except _BROWSER_ERRORS as e:
            logger.debug("initial nav to {} ignored ({})", _HOME_URL, e)
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        logger.info("LiveBrowser {} started (cdp_url={})", self.browser_id, cdp_url)

    # --- screencast / active tab ---------------------------------------------

    async def _set_active_page(self, page: Page) -> None:
        """Point the screencast at ``page`` and make it the input/agent target.

        Serialized by ``_lock`` so overlapping calls (rapid navigations each firing
        framenavigated) can't interleave at the stop/attach boundary and leak a CDP
        session or start two screencasts on one target.
        """
        async with self._lock:
            if self._context is None:
                return  # torn down -- close() raced a queued nav re-attach
            if self._active_cdp is not None:
                await self._stop_screencast()
            self._active_page = page
            try:
                cdp = await self._context.new_cdp_session(page)
                self._active_cdp = cdp
                try:
                    info = await cdp.send("Target.getTargetInfo")
                    self._active_target_id = info["targetInfo"]["targetId"]
                except _BROWSER_ERRORS:
                    self._active_target_id = None
                cdp.on("Page.screencastFrame", self._on_screencast_frame)
                await cdp.send(
                    "Page.startScreencast",
                    {
                        "format": _SCREENCAST_FORMAT,
                        "quality": _SCREENCAST_QUALITY,
                        "maxWidth": _SCREENCAST_MAX_WIDTH,
                        "maxHeight": _SCREENCAST_MAX_HEIGHT,
                        "everyNthFrame": _SCREENCAST_EVERY_NTH_FRAME,
                    },
                )
            except _BROWSER_ERRORS as e:
                logger.debug("screencast attach ignored ({})", e)
                return
        await self._broadcast_tabs()

    async def _stop_screencast(self) -> None:
        cdp = self._active_cdp
        self._active_cdp = None
        if cdp is None:
            return
        try:
            await cdp.send("Page.stopScreencast")
            await cdp.detach()
        except _BROWSER_ERRORS as e:
            logger.debug("screencast stop ignored ({})", e)

    def _on_new_page(self, page: Page) -> None:
        """A new tab appeared (human or agent opened it): follow it."""
        asyncio.create_task(self._follow_new_page(page))

    async def _follow_new_page(self, page: Page) -> None:
        page.on("close", lambda _p: asyncio.create_task(self._broadcast_tabs()))
        self._track_nav(page)
        try:
            await page.wait_for_load_state("domcontentloaded")
            await self._set_active_page(page)
        except _BROWSER_ERRORS as e:
            logger.debug("follow new page ignored ({})", e)

    def _track_nav(self, page: Page) -> None:
        """Re-point the screencast + refresh tabs whenever the active page navigates.

        A screencast is bound to one CDP target; a cross-origin navigation swaps the
        target and silently stops the old screencast, so without this the view freezes
        on the old page and the URL bar goes stale. Re-running _set_active_page rebinds
        to the page's current target and re-broadcasts the tab list.
        """
        if page in self._nav_tracked:
            return
        self._nav_tracked.add(page)
        page.on("framenavigated", lambda frame, captured=page: self._on_page_nav(frame, captured))

    def _on_page_nav(self, frame: Any, page: Page) -> None:
        if page is self._active_page and frame == page.main_frame:
            asyncio.create_task(self._set_active_page(page))

    def _on_screencast_frame(self, params: dict[str, Any]) -> None:
        """Playwright (sync) callback: stash the frame and schedule ack + send."""
        self._latest_frame = params.get("data")
        session_id = params.get("sessionId")
        asyncio.create_task(self._ack_and_send(session_id))

    async def _ack_and_send(self, screencast_session_id: Any) -> None:
        cdp = self._active_cdp
        if cdp is None:
            return
        try:
            await cdp.send("Page.screencastFrameAck", {"sessionId": screencast_session_id})
        except _BROWSER_ERRORS as e:
            logger.debug("screencast ack ignored ({})", e)
            return
        if self._send_in_flight:
            return
        self._send_in_flight = True
        try:
            frame = self._latest_frame
            if frame is not None:
                await self._broadcast({"type": "frame", "data": frame})
        finally:
            self._send_in_flight = False

    async def _broadcast_tabs(self) -> None:
        await self._broadcast({"type": "tabs", "tabs": await self._tab_list()})

    async def _tab_list(self) -> list[dict[str, Any]]:
        if self._context is None:
            return []
        tabs = []
        for index, page in enumerate(self._context.pages):
            tabs.append(
                {
                    "index": index,
                    "title": (await _safe_title(page)),
                    "url": page.url,
                    "active": page is self._active_page,
                }
            )
        return tabs

    async def _follow_agent_focus(self) -> None:
        """Re-point the screencast to the tab the agent just switched to.

        New tabs and navigations are already followed (``_on_new_page`` /
        ``framenavigated``); this covers the agent activating an already-open
        background tab (``switch_tab``), which fires neither. We match
        browser-use's focused CDP target to one of the observer's pages by URL;
        same-URL tabs are an acceptable ambiguity.
        """
        focus_id = getattr(self._bu_session, "agent_focus_target_id", None)
        cdp, context = self._active_cdp, self._context
        if not focus_id or focus_id == self._active_target_id or cdp is None or context is None:
            return
        try:
            targets = (await cdp.send("Target.getTargets")).get("targetInfos", [])
        except _BROWSER_ERRORS as e:
            logger.debug("getTargets for focus-follow ignored ({})", e)
            return
        focus_url = next(
            (t["url"] for t in targets if t.get("targetId") == focus_id and t.get("type") == "page"),
            None,
        )
        if focus_url is None:
            return
        for page in context.pages:
            if page.url == focus_url and page is not self._active_page:
                await self._set_active_page(page)
                return

    async def _keepalive_loop(self) -> None:
        """Ping cast sockets periodically so a static page (no screencast frames)
        doesn't let the WS proxy time out the idle stream; also sweep idle leases."""
        while not self._closed:
            await asyncio.sleep(_KEEPALIVE_SECONDS)
            await self._broadcast({"type": "ping"})
            await self._sweep_idle_lease()

    async def _sweep_idle_lease(self) -> bool:
        """Release a direct-control lease whose owner has gone quiet (dead/wandered-off
        agent). A running ``task`` (``_agent_task`` set) is connection-bound and exempt;
        the CAS keeps this from clobbering a freshly-handed-off lease. Returns True if it
        released one."""
        if (
            self.controller == "agent"
            and self._agent_task is None
            and time.monotonic() - self._lease_touched_at > _LEASE_IDLE_TTL
        ):
            return await self._transition(to="human", expect=("agent", self.owner_agent_id, False))
        return False

    # --- input ----------------------------------------------------------------

    async def handle_cast_message(self, message: dict[str, Any]) -> None:
        """Handle a message from a cast socket: human input or tab control.

        Input/tab/nav are gated on ``_input_enabled`` (set only while the human has
        control). The check and the CDP dispatch happen together under
        ``_control_lock`` so an agent acquiring the browser mid-dispatch can't let a
        stale human input land after the handoff (the input/control TOCTOU).
        """
        kind = message.get("type")
        if kind in ("mouse", "key", "tab", "navigate"):
            async with self._control_lock:
                if not self._input_enabled.is_set():
                    return
                await self._dispatch_input(message)

    async def _dispatch_input(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        try:
            cdp = self._active_cdp
            if kind == "mouse" and cdp is not None:
                await cdp.send("Input.dispatchMouseEvent", message["event"])
            elif kind == "key" and cdp is not None:
                await cdp.send("Input.dispatchKeyEvent", message["event"])
            elif kind == "tab":
                await self._handle_tab_control(message)
            elif kind == "navigate" and self._active_page is not None:
                await self._active_page.goto(message["url"])
        except _BROWSER_ERRORS as e:
            logger.debug("cast input ignored ({})", e)

    async def _handle_tab_control(self, message: dict[str, Any]) -> None:
        if self._context is None:
            return
        action = message.get("action")
        if action == "new":
            page = await self._context.new_page()
            await page.goto(message.get("url") or _HOME_URL)
        elif action == "activate":
            index = int(message.get("index", 0))
            if 0 <= index < len(self._context.pages):
                page = self._context.pages[index]
                await page.bring_to_front()
                await self._set_active_page(page)
        elif action == "close":
            index = int(message.get("index", 0))
            if 0 <= index < len(self._context.pages):
                await self._context.pages[index].close()

    # --- ownership state machine ----------------------------------------------

    def _state_tuple(self) -> tuple[ControlOwner, str | None, bool]:
        return (self.controller, self.owner_agent_id, self.human_pinned)

    async def _write_control_locked(
        self, to: ControlOwner, agent_id: str | None, agent_name: str | None, pinned: bool
    ) -> None:
        """The ONLY writer of control state. Caller must hold ``_control_lock``.

        Writes ``controller``/``owner_agent_id``/``human_pinned`` and ``_input_enabled``
        together (so the input gate can never disagree with the controller), then
        broadcasts the new state to every cast socket and stores it as the current
        state for ``send_initial_state`` to replay to late joiners.
        """
        self.controller = to
        self.owner_agent_id = agent_id
        self.owner_agent_name = agent_name
        self.human_pinned = pinned
        if to == "human":
            self._input_enabled.set()
        else:
            self._input_enabled.clear()
            self._lease_touched_at = time.monotonic()  # start the sticky-lease idle clock
        await self._broadcast(self._control_message())

    def _control_message(self) -> dict[str, Any]:
        return {
            "type": "control",
            "owner": self.controller,
            "owner_agent_id": self.owner_agent_id,
            "owner_name": self.owner_agent_name,
            "human_pinned": self.human_pinned,
        }

    def _control_state(self) -> dict[str, Any]:
        """Owner snapshot embedded in every direct-command response so the agent can
        tell, after each call, whether it still holds control (e.g. a human took it)."""
        return {
            "controller": self.controller,
            "owner_agent_id": self.owner_agent_id,
            "owner_name": self.owner_agent_name,
            "human_pinned": self.human_pinned,
        }

    async def _settle_queue_locked(self) -> None:
        """Reconcile the FIFO wait-queue with the current control state. Holds ``_control_lock``.

        * human-pinned -> evict every waiter (agents never wait on a human).
        * free (unpinned human) -> hand the browser to the first waiter, gaplessly.
        * agent-owned -> nothing (someone holds it; waiters stay queued).
        """
        if self.controller == "human" and self.human_pinned:
            waiters, self._wait_queue = self._wait_queue, []
            for waiter in waiters:
                waiter.granted = False
                waiter.event.set()
        elif self.controller == "human" and self._wait_queue:
            waiter = self._wait_queue.pop(0)
            await self._write_control_locked("agent", waiter.agent_id, waiter.agent_name, pinned=False)
            waiter.granted = True
            waiter.event.set()

    async def _transition(
        self,
        *,
        to: ControlOwner,
        agent_id: str | None = None,
        agent_name: str | None = None,
        pinned: bool = False,
        expect: tuple[ControlOwner, str | None, bool] | None = None,
        preempt: bool = False,
    ) -> bool:
        """Atomic compare-and-set control transition (the single mutation path).

        Returns False (and changes nothing) if ``expect`` is given and the current
        state differs -- this is how a stale finally / double-release no-ops safely.
        When ``preempt`` is set, the displaced agent's run is cancelled OUTSIDE the
        lock and never awaited here: the cancelled run's own finally re-enters this
        method, CAS-fails (state already moved on), and no-ops -- so there is no
        lock cycle (the deadlock the audit warned about).
        """
        displaced_agent: Agent | None = None
        displaced_task: "asyncio.Task[None] | None" = None
        async with self._control_lock:
            if expect is not None and self._state_tuple() != expect:
                return False
            if preempt:
                displaced_agent = self._agent
                displaced_task = self._agent_task
            await self._write_control_locked(to, agent_id, agent_name, pinned)
            await self._settle_queue_locked()
        if displaced_agent is not None:
            displaced_agent.stop()
        if displaced_task is not None and displaced_task is not asyncio.current_task() and not displaced_task.done():
            displaced_task.cancel()
        return True

    async def acquire(
        self,
        agent_id: str,
        agent_name: str | None = None,
        *,
        reclaim: bool = False,
        wait: bool = True,
        max_wait: float | None = None,
        on_wait: Callable[[str | None, str | None], Awaitable[None]] | None = None,
    ) -> str:
        """Acquire control for an agent. Returns one of:

        ``"acquired"`` -- the agent now controls the browser.
        ``"busy_human"`` -- a human holds it (pinned); only an explicit ``reclaim``
            (the human told the agent to resume) takes it. Agents never wait on a human.
        ``"busy_agent"`` -- another agent holds it and ``wait`` was False.
        ``"timed_out"`` -- waited ``max_wait`` seconds for another agent to release.

        With ``wait`` (the default) and another agent in control, the caller parks in
        a FIFO queue and is handed the browser the instant that agent releases.
        """
        async with self._control_lock:
            if self.controller == "agent" and self.owner_agent_id == agent_id:
                self.owner_agent_name = agent_name  # refresh display name on re-acquire
                return "acquired"
            if self.controller == "human" and self.human_pinned and not reclaim:
                return "busy_human"
            if self.controller == "human":  # free, or reclaim of a pinned human
                await self._write_control_locked("agent", agent_id, agent_name, pinned=False)
                return "acquired"
            # controller == "agent", a different agent -> must wait or fail fast.
            if not wait:
                return "busy_agent"
            busy_id, busy_name = self.owner_agent_id, self.owner_agent_name
            waiter = _AcquireWaiter(agent_id, agent_name)
            self._wait_queue.append(waiter)
        if on_wait is not None:
            await on_wait(busy_id, busy_name)
        try:
            await asyncio.wait_for(waiter.event.wait(), timeout=max_wait)
        except (TimeoutError, asyncio.CancelledError) as exc:
            async with self._control_lock:
                if waiter in self._wait_queue:
                    self._wait_queue.remove(waiter)
                elif waiter.granted and self.controller == "agent" and self.owner_agent_id == agent_id:
                    # Handed the browser concurrently with our give-up: release it so
                    # the next waiter (or the human) isn't blocked by a no-show owner.
                    await self._write_control_locked("human", None, None, pinned=False)
                    await self._settle_queue_locked()
            if isinstance(exc, asyncio.CancelledError):
                raise
            return "timed_out"
        return "acquired" if waiter.granted else "busy_human"

    async def release(self, agent_id: str) -> bool:
        """Release this agent's control back to the human (free). CAS: only the owner can."""
        return await self._transition(to="human", expect=("agent", agent_id, False))

    async def take_control(self) -> bool:
        """Human 'take control': preempt whatever agent is driving and pin (agents locked out).

        Always wins (no ``expect``): flips to a pinned human and cancels the run. The
        cancel happens outside the control lock, so the run's finally can re-enter the
        state machine without deadlocking. No resume: the human hands back via
        :meth:`return_to_agents`, or tells an agent to resume (which uses ``reclaim``).
        """
        return await self._transition(to="human", pinned=True, preempt=True)

    async def return_to_agents(self) -> bool:
        """Human hands control back: un-pin (only if currently pinned). Frees any waiter."""
        return await self._transition(to="human", pinned=False, expect=("human", None, True))

    async def run_agent(self, prompt: str, on_event: EventSink) -> None:
        """Run a browser-use task against this (already-acquired) browser, streaming steps.

        Ownership is managed by the caller (the task endpoint acquires before and
        releases after); this method only drives browser-use and reports events.
        """
        api_key = resolve_anthropic_key()
        if not api_key:
            await on_event({"type": "error", "text": anthropic_key_status()[1]})
            return
        self._run_on_event = on_event
        self._agent_task = asyncio.current_task()
        # Key is passed straight to ChatAnthropic -- never into os.environ, which would
        # leak across the manager's concurrent sessions and race between runs.
        agent = Agent(
            task=prompt,
            llm=ChatAnthropic(model=_DEFAULT_MODEL, api_key=api_key),
            browser_session=self._bu_session,
        )
        self._agent = agent
        try:
            await asyncio.wait_for(
                agent.run(on_step_end=self._on_agent_step, max_steps=_TASK_MAX_STEPS),
                timeout=_TASK_MAX_SECONDS,
            )
            summary = agent.history.final_result()
            await on_event({"type": "done", "result": summary or "Done."})
        except asyncio.CancelledError:
            await on_event({"type": "preempted"})
            raise
        except TimeoutError:
            agent.stop()
            await on_event({"type": "error", "text": f"Task exceeded {_TASK_MAX_SECONDS:.0f}s and was stopped."})
        except Exception as e:  # noqa: BLE001 -- surface any agent failure to the caller's stream
            logger.opt(exception=e).error("browser-use agent run failed for browser {}", self.browser_id)
            await on_event({"type": "error", "text": f"Agent error: {e}"})
        finally:
            if self._agent is agent:
                self._agent = None
                self._agent_task = None
                self._run_on_event = None

    async def _on_agent_step(self, agent: Agent) -> None:
        """browser-use per-step hook: stream the latest thought + action as separate events."""
        emit = self._run_on_event
        if emit is None:
            return
        history = agent.history
        thoughts = history.model_thoughts()
        actions = history.model_actions()
        if thoughts:
            thought = thoughts[-1]
            summary = str(
                getattr(thought, "next_goal", "")
                or getattr(thought, "evaluation_previous_goal", "")
                or "Thinking"
            ).strip()
            detail = str(getattr(thought, "thinking", "") or thought).strip()
            await emit({"type": "thinking", "text": summary, "detail": detail})
        if actions:
            action = actions[-1]
            await emit(
                {"type": "action", "text": _action_summary(action), "detail": json.dumps(action, indent=2, default=str)}
            )
        # Keep the streamed view on whatever tab the agent is now focused on.
        await self._follow_agent_focus()

    async def _stop_active_agent(self) -> None:
        """Stop any running agent and wait for its run task to unwind (used by close())."""
        agent = self._agent
        task = self._agent_task
        if agent is not None:
            agent.stop()
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, *_BROWSER_ERRORS):
                pass

    # --- direct control (Claude drives the browser itself, one command at a time) ---

    def _ensure_action_handler(self) -> ActionHandler:
        """browser-use's own action executor, bound once to our held BrowserSession."""
        if self._action_handler is None:
            self._action_handler = ActionHandler(self._bu_session)
        return self._action_handler

    async def run_action(
        self, agent_id: str, agent_name: str | None, action: Callable[[ActionHandler], Awaitable[dict[str, Any]]]
    ) -> dict[str, Any]:
        """Run one direct-control action for an agent, returning a result + owner snapshot.

        Ownership is a sticky lease: the first action acquires the browser (CAS, no
        wait -- a busy browser fails fast rather than blocking a click), later actions
        refresh it. The CRITICAL guard is the per-command compare-and-set: right before
        the browser action we re-check ``(agent, me, unpinned)`` under ``_control_lock``,
        so a human take-control between two commands makes the next one a clean no-op
        (``lost_control``) instead of touching the human's browser. The action itself
        runs under ``_lock`` (serialized with screencast tab-switches), NOT under
        ``_control_lock`` -- so a human take-control stays instant (at worst one
        in-flight action lands before the next command sees it).
        """
        status = await self.acquire(agent_id, agent_name, wait=False)
        if status != "acquired":
            return {"ok": False, "status": status, **self._control_state()}
        async with self._control_lock:
            if self._state_tuple() != ("agent", agent_id, False):
                return {"ok": False, "status": "lost_control", **self._control_state()}
            self._lease_touched_at = time.monotonic()
        async with self._lock:
            if self._context is None:
                return {"ok": False, "status": "closed", **self._control_state()}
            try:
                result = await action(self._ensure_action_handler())
            except _BROWSER_ERRORS as e:
                logger.debug("direct action failed on browser {} ({})", self.browser_id, e)
                return {"ok": False, "status": "error", "error": str(e), **self._control_state()}
        return {"ok": True, "status": "ok", **result, **self._control_state()}

    def _node(self, index: int) -> Any:
        """Resolve an element index from the last ``state`` snapshot to its DOM node."""
        return self._selector_map.get(index)

    async def act_state(self, agent_id: str, agent_name: str | None) -> dict[str, Any]:
        async def _do(handler: ActionHandler) -> dict[str, Any]:
            summary = await handler.get_state()
            self._selector_map = dict(getattr(summary.dom_state, "selector_map", {}) or {})
            elements = summary.dom_state.llm_representation()
            return {"url": summary.url, "title": summary.title, "elements": elements, "tabs": await self._tab_list()}

        return await self.run_action(agent_id, agent_name, _do)

    async def act_navigate(self, agent_id: str, agent_name: str | None, url: str) -> dict[str, Any]:
        async def _do(handler: ActionHandler) -> dict[str, Any]:
            await handler.navigate(url)
            self._selector_map = {}  # page changed -- old element indices are void
            return {"navigated": url}

        return await self.run_action(agent_id, agent_name, _do)

    async def act_click(self, agent_id: str, agent_name: str | None, index: int) -> dict[str, Any]:
        async def _do(handler: ActionHandler) -> dict[str, Any]:
            node = self._node(index)
            if node is None:
                return {"ok": False, "status": "stale_index", "error": f"no element {index}; run `state` first (the page may have changed)"}
            await handler.click_element(node)
            self._selector_map = {}  # a click may navigate/mutate -- force a re-`state`
            return {"clicked": index}

        return await self.run_action(agent_id, agent_name, _do)

    async def act_input(self, agent_id: str, agent_name: str | None, index: int, text: str) -> dict[str, Any]:
        async def _do(handler: ActionHandler) -> dict[str, Any]:
            node = self._node(index)
            if node is None:
                return {"ok": False, "status": "stale_index", "error": f"no element {index}; run `state` first"}
            await handler.type_text(node, text)
            return {"typed_into": index}

        return await self.run_action(agent_id, agent_name, _do)

    async def act_select(self, agent_id: str, agent_name: str | None, index: int, value: str) -> dict[str, Any]:
        async def _do(handler: ActionHandler) -> dict[str, Any]:
            node = self._node(index)
            if node is None:
                return {"ok": False, "status": "stale_index", "error": f"no element {index}; run `state` first"}
            await handler.select_dropdown(node, value)
            return {"selected": value, "index": index}

        return await self.run_action(agent_id, agent_name, _do)

    async def act_scroll(self, agent_id: str, agent_name: str | None, direction: str, amount: int) -> dict[str, Any]:
        async def _do(handler: ActionHandler) -> dict[str, Any]:
            await handler.scroll(direction, amount)
            self._selector_map = {}
            return {"scrolled": direction}

        return await self.run_action(agent_id, agent_name, _do)

    async def act_keys(self, agent_id: str, agent_name: str | None, keys: str) -> dict[str, Any]:
        async def _do(handler: ActionHandler) -> dict[str, Any]:
            await handler.send_keys(keys)
            return {"keys": keys}

        return await self.run_action(agent_id, agent_name, _do)

    async def act_screenshot(self, agent_id: str, agent_name: str | None) -> dict[str, Any]:
        async def _do(_handler: ActionHandler) -> dict[str, Any]:
            data = await self._bu_session.take_screenshot()
            raw = data if isinstance(data, (bytes, bytearray)) else base64.b64decode(data)
            _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
            self._screenshot_seq += 1
            path = _SCREENSHOT_DIR / f"browser-{self.browser_id}-{self._screenshot_seq}.png"
            path.write_bytes(raw)
            return {"screenshot_path": str(path.resolve())}

        return await self.run_action(agent_id, agent_name, _do)

    async def act_tab(self, agent_id: str, agent_name: str | None, action: str, index: int | None, url: str | None) -> dict[str, Any]:
        async def _do(_handler: ActionHandler) -> dict[str, Any]:
            # Tabs go through OUR Playwright context (same path as the human's tab bar),
            # so the screencast follows the switch -- not browser-use's separate notion.
            # "list" is a read-only no-op here; the tab list is returned below.
            if action in ("activate", "new", "close"):
                await self._handle_tab_control({"action": action, "index": index or 0, "url": url})
                self._selector_map = {}
            return {"tab_action": action, "tabs": await self._tab_list()}

        return await self.run_action(agent_id, agent_name, _do)

    # --- socket bookkeeping ---------------------------------------------------

    def add_cast_socket(self, ws: WebSocket) -> None:
        self._cast_sockets.append(ws)

    def remove_cast_socket(self, ws: WebSocket) -> None:
        if ws in self._cast_sockets:
            self._cast_sockets.remove(ws)

    async def send_initial_state(self, ws: WebSocket) -> None:
        """Send current control + tab state to a freshly-connected cast socket (initial sync)."""
        await ws.send_json(self._control_message())
        await ws.send_json({"type": "tabs", "tabs": await self._tab_list()})

    async def describe(self) -> dict[str, Any]:
        """Snapshot for ``GET /browsers``: id, owner, and the current tab list."""
        return {
            "id": self.browser_id,
            "controller": self.controller,
            "owner_agent_id": self.owner_agent_id,
            "owner_name": self.owner_agent_name,
            "human_pinned": self.human_pinned,
            "tabs": await self._tab_list(),
        }

    async def _broadcast(self, message: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for ws in list(self._cast_sockets):
            try:
                await ws.send_json(message)
            except _BROWSER_ERRORS as e:
                logger.debug("dropping dead socket ({})", e)
                dead.append(ws)
        for ws in dead:
            if ws in self._cast_sockets:
                self._cast_sockets.remove(ws)

    async def close(self) -> None:
        self._closed = True
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
        # Evict any waiters so their task endpoints unblock instead of hanging on a dead browser.
        async with self._control_lock:
            waiters, self._wait_queue = self._wait_queue, []
        for waiter in waiters:
            waiter.granted = False
            waiter.event.set()
        await self._stop_active_agent()
        await self._stop_screencast()
        self._context = None  # bail out any nav re-attach queued during teardown
        if self._observer is not None:
            try:
                await self._observer.close()
            except _BROWSER_ERRORS as e:
                logger.debug("observer close ignored ({})", e)
        bu_session = getattr(self, "_bu_session", None)
        if bu_session is not None:
            try:
                await bu_session.kill()
            except _BROWSER_ERRORS as e:
                logger.debug("browser kill ignored ({})", e)


async def _safe_title(page: Page) -> str:
    try:
        return await page.title()
    except _BROWSER_ERRORS:
        return page.url


class BrowserSessionManager(MutableModel):
    """Owns the whole fleet (all live browsers) and the shared Playwright driver.

    The fleet is shared per workspace: every agent in a mind reaches this one
    manager, so ``ls`` shows one fleet and ownership arbitrates between agents.
    Browser ids are monotonic and never reused (``_next_id`` only increases);
    id 0 is the permanent default, re-createable via :meth:`ensure_browser_0`.
    """

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    _browsers: dict[int, LiveBrowser] = PrivateAttr(default_factory=dict)
    _playwright: Playwright | None = PrivateAttr(default=None)
    _next_id: int = PrivateAttr(default=1)  # 0 is reserved for the default browser
    _lock: asyncio.Lock = PrivateAttr(default_factory=asyncio.Lock)

    async def _start_and_register_locked(self, browser_id: int) -> LiveBrowser:
        """Launch + register one browser. Caller must hold ``self._lock`` for the whole
        call so a concurrent create can't observe a stale count (no cap overshoot) or
        race the id assignment."""
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        session = LiveBrowser(browser_id=browser_id)
        started = False
        try:
            await session.start(self._playwright)
            started = True
        finally:
            if not started:
                await session.close()  # start() failed partway -- don't leak a Chromium
        self._browsers[browser_id] = session
        return session

    async def create(self) -> LiveBrowser:
        """Start a new browser with the next monotonic id ('New browser' / fleet ``new``)."""
        async with self._lock:
            if len(self._browsers) >= _MAX_SESSIONS:
                raise FleetFullError(
                    f"Too many open browsers ({len(self._browsers)}/{_MAX_SESSIONS}). "
                    "Close one before opening another."
                )
            browser_id = self._next_id
            self._next_id += 1
            return await self._start_and_register_locked(browser_id)

    async def ensure_browser_0(self) -> LiveBrowser:
        """Return the default browser (id 0), creating it if absent. Idempotent under the lock."""
        async with self._lock:
            existing = self._browsers.get(0)
            if existing is not None:
                return existing
            return await self._start_and_register_locked(0)

    def get(self, browser_id: int) -> LiveBrowser:
        # Dict access raises KeyError for a missing/closed id; callers turn it into a 404.
        return self._browsers[browser_id]

    async def list_browsers(self) -> list[dict[str, Any]]:
        return [await self._browsers[bid].describe() for bid in sorted(self._browsers)]

    async def close(self, browser_id: int) -> None:
        session = self._browsers.pop(browser_id, None)
        if session is not None:
            await session.close()

    async def shutdown(self) -> None:
        for browser_id in list(self._browsers):
            await self.close(browser_id)
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
