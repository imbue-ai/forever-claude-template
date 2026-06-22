"""Live browser sessions: headful Chromium + CDP screencast + human/agent control.

Each :class:`LiveBrowser` owns one headful Chromium (launched and driven by
``browser_use.BrowserSession``) plus a second, observer-only Playwright
connection over the same CDP endpoint. The Playwright side does the things
browser-use does not: stream the live view to the user (CDP
``Page.startScreencast`` -> base64 JPEG frames over a WebSocket) and inject the
user's mouse/keyboard (CDP ``Input.dispatch*Event``). The browser-use side does
the AI driving (``Agent.run``). Both clients share the one Chromium, so the
human sees exactly what the agent does and vice versa.

Control is a single flag (``control_owner``). When the human has control, the
cast socket's input is dispatched; when the agent has control, human input is
dropped (``_input_enabled`` cleared) and browser-use drives. "Take control"
pauses the agent and hands the flag back.

Credentials for browser-use are read lazily from the environment (and a fresh
re-read of ``$MNGR_HOST_DIR/env``) at run time, so a key submitted after this
service booted is still picked up without a restart. The Anthropic SDK inside
``ChatAnthropic`` reads ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_BASE_URL`` from the
environment, so the proxy (Imbue Cloud) and direct-key cases both work.
"""

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any
from typing import Literal

from browser_use import Agent
from browser_use import BrowserSession
from browser_use import ChatAnthropic
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
_MAX_SESSIONS = int(os.environ.get("BROWSER_MAX_SESSIONS", "3"))


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


def resolve_anthropic_credentials() -> tuple[str | None, str | None]:
    """Return ``(api_key, base_url)`` from the process env, falling back to ``$MNGR_HOST_DIR/env``.

    The fallback re-reads the host env file fresh so a key submitted after this
    service started is still found without a restart.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    base_url = os.environ.get("ANTHROPIC_BASE_URL")
    if not api_key:
        host_dir = os.environ.get("MNGR_HOST_DIR")
        if host_dir:
            env_path = Path(host_dir) / "env"
            if env_path.exists():
                parsed = _parse_env_file(env_path.read_text())
                api_key = parsed.get("ANTHROPIC_API_KEY")
                base_url = base_url or parsed.get("ANTHROPIC_BASE_URL")
    return api_key, base_url


def anthropic_key_status() -> tuple[bool, str]:
    """Return ``(available, reason)`` for gating the "New browser" menu item."""
    api_key, _ = resolve_anthropic_credentials()
    if api_key:
        return True, "Anthropic API key available"
    return (
        False,
        "Browser sessions need an Anthropic API key. Create the workspace with the "
        "'Anthropic API key' or 'Imbue Cloud' provider (the 'Claude subscription' "
        "option has no usable key for browser automation).",
    )


def deferred_install_ready() -> tuple[bool, str]:
    """Return ``(ready, reason)`` once Chromium is installed."""
    if os.environ.get("BROWSER_SKIP_INSTALL_CHECK") == "1":
        return True, "ready"  # host/CI testing without the deferred-install marker
    if not _PLAYWRIGHT_MARKER.exists():
        return False, "Chromium is still installing in this workspace; try again in a minute."
    return True, "ready"


class BrowserStartupError(Exception):
    """Raised when a Chromium session fails to come up (e.g. no CDP endpoint)."""


class LiveBrowser(MutableModel):
    """One headful Chromium streamed to the user, optionally driven by a browser-use agent."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    session_id: str
    control_owner: ControlOwner = "human"

    _playwright: Playwright = PrivateAttr()
    _bu_session: BrowserSession = PrivateAttr()
    _observer: Browser | None = PrivateAttr(default=None)
    _context: BrowserContext | None = PrivateAttr(default=None)
    _active_page: Page | None = PrivateAttr(default=None)
    _active_cdp: CDPSession | None = PrivateAttr(default=None)
    _agent: Agent | None = PrivateAttr(default=None)
    _agent_task: "asyncio.Task[None] | None" = PrivateAttr(default=None)
    _input_enabled: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)
    _cast_sockets: list[WebSocket] = PrivateAttr(default_factory=list)
    _chat_sockets: list[WebSocket] = PrivateAttr(default_factory=list)
    _latest_frame: str | None = PrivateAttr(default=None)
    _send_in_flight: bool = PrivateAttr(default=False)
    _nav_tracked: set[Page] = PrivateAttr(default_factory=set)
    _active_target_id: str | None = PrivateAttr(default=None)
    _queued_prompt: str | None = PrivateAttr(default=None)
    _run_active: bool = PrivateAttr(default=False)
    _keepalive_task: "asyncio.Task[None] | None" = PrivateAttr(default=None)
    _closed: bool = PrivateAttr(default=False)
    _lock: asyncio.Lock = PrivateAttr(default_factory=asyncio.Lock)

    async def start(self, playwright: Playwright) -> None:
        """Launch the headful Chromium (browser-use) and attach the Playwright observer."""
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
        logger.info("LiveBrowser {} started (cdp_url={})", self.session_id, cdp_url)

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
        if self._context is None:
            return
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
        await self._broadcast({"type": "tabs", "tabs": tabs})

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
        doesn't let the WS proxy time out the idle stream."""
        while not self._closed:
            await asyncio.sleep(_KEEPALIVE_SECONDS)
            await self._broadcast({"type": "ping"})

    # --- input ----------------------------------------------------------------

    async def handle_cast_message(self, message: dict[str, Any]) -> None:
        """Handle a message from a cast socket: human input or tab control."""
        kind = message.get("type")
        # While the agent drives, drop ALL human browser control (input + tabs + nav).
        if kind in ("mouse", "key", "tab", "navigate") and not self._input_enabled.is_set():
            return
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

    # --- agent control --------------------------------------------------------

    async def run_agent(self, prompt: str) -> None:
        """Run a browser-use task against this browser, streaming steps to chat sockets.

        Single-flight: any agent already running or paused is stopped first, so only
        one agent ever drives the shared browser.
        """
        api_key, base_url = resolve_anthropic_credentials()
        if not api_key:
            await self._broadcast({"type": "error", "text": anthropic_key_status()[1]}, chat=True)
            return
        await self._stop_active_agent()
        self._agent_task = asyncio.current_task()
        await self._set_control("agent")
        await self._broadcast({"type": "chat", "role": "user", "text": prompt}, chat=True)
        # Credentials go straight to the client -- never into os.environ, which would
        # leak across the manager's concurrent sessions and race between runs.
        agent = Agent(
            task=prompt,
            llm=ChatAnthropic(model=_DEFAULT_MODEL, api_key=api_key, base_url=base_url),
            browser_session=self._bu_session,
        )
        self._agent = agent
        try:
            await agent.run(on_step_end=self._on_agent_step)
            summary = agent.history.final_result()
            await self._broadcast({"type": "chat", "role": "assistant", "text": summary or "Done."}, chat=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 -- surface any agent failure to the user's chat
            logger.opt(exception=e).error("browser-use agent run failed for {}", self.session_id)
            await self._broadcast({"type": "error", "text": f"Agent error: {e}"}, chat=True)
        finally:
            if self._agent is agent:
                self._agent = None
                self._agent_task = None
                await self._set_control("human")

    async def _on_agent_step(self, agent: Agent) -> None:
        """browser-use per-step hook: stream the latest thought + action as separate collapsible blocks."""
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
            await self._broadcast({"type": "chat", "role": "thinking", "text": summary, "detail": detail}, chat=True)
        if actions:
            action = actions[-1]
            await self._broadcast(
                {"type": "chat", "role": "action", "text": _action_summary(action), "detail": json.dumps(action, indent=2, default=str)},
                chat=True,
            )
        # Keep the streamed view on whatever tab the agent is now focused on.
        await self._follow_agent_focus()

    async def submit(self, prompt: str) -> None:
        """Start an agent run, or queue the prompt if a run is already in progress.

        While the agent has control the user can't start a second concurrent run;
        the message waits and auto-runs when the current task finishes. ``_run_active``
        is set synchronously here so a prompt sent in the brief window before the
        Agent object is assigned still queues instead of starting a parallel run.
        """
        if self._run_active:
            self._queued_prompt = prompt
            await self._broadcast({"type": "queued", "text": prompt}, chat=True)
            return
        self._run_active = True
        asyncio.create_task(self._run_chain(prompt))

    async def _run_chain(self, prompt: str | None) -> None:
        """Run prompts to completion one at a time, draining a queued follow-up after each."""
        try:
            while prompt is not None:
                await self.run_agent(prompt)
                prompt = self._queued_prompt
                if prompt is not None:
                    self._queued_prompt = None
                    await self._broadcast({"type": "queued", "text": None}, chat=True)
        finally:
            self._run_active = False

    async def cancel_queue(self) -> None:
        """Drop the pending queued message (user cancelled the chip)."""
        self._queued_prompt = None
        await self._broadcast({"type": "queued", "text": None}, chat=True)

    async def take_control(self) -> None:
        """'Take control': stop the agent completely and give control to the human.

        There is no resume -- to continue, the user sends a new message (a fresh
        run on the current browser state). Any queued message is dropped.
        """
        self._queued_prompt = None
        await self._stop_active_agent()
        await self._set_control("human")
        await self._broadcast({"type": "queued", "text": None}, chat=True)

    async def _stop_active_agent(self) -> None:
        """Stop any running agent and wait for its run task to unwind."""
        agent = self._agent
        task = self._agent_task
        if agent is not None:
            agent.stop()
        if task is not None and task is not asyncio.current_task():
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _set_control(self, owner: ControlOwner) -> None:
        self.control_owner = owner
        # Human input flows in every state except while the agent is actively driving.
        if owner == "agent":
            self._input_enabled.clear()
        else:
            self._input_enabled.set()
        # Broadcast on BOTH sockets: the cast socket may be mid-reconnect when the
        # agent starts, so relying on it alone would leave the status box stale.
        await self._broadcast({"type": "control", "owner": owner})
        await self._broadcast({"type": "control", "owner": owner}, chat=True)

    # --- socket bookkeeping ---------------------------------------------------

    def add_cast_socket(self, ws: WebSocket) -> None:
        self._cast_sockets.append(ws)

    def remove_cast_socket(self, ws: WebSocket) -> None:
        if ws in self._cast_sockets:
            self._cast_sockets.remove(ws)

    def add_chat_socket(self, ws: WebSocket) -> None:
        self._chat_sockets.append(ws)

    def remove_chat_socket(self, ws: WebSocket) -> None:
        if ws in self._chat_sockets:
            self._chat_sockets.remove(ws)

    async def send_initial_state(self, ws: WebSocket) -> None:
        """Send current control + tab state to a freshly-connected cast socket."""
        await ws.send_json({"type": "control", "owner": self.control_owner})
        await self._broadcast_tabs()

    async def _broadcast(self, message: dict[str, Any], chat: bool = False) -> None:
        sockets = self._chat_sockets if chat else self._cast_sockets
        dead: list[WebSocket] = []
        for ws in list(sockets):
            try:
                await ws.send_json(message)
            except _BROWSER_ERRORS as e:
                logger.debug("dropping dead socket ({})", e)
                dead.append(ws)
        for ws in dead:
            if ws in sockets:
                sockets.remove(ws)

    async def close(self) -> None:
        self._closed = True
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
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
    """Owns all live browsers and the shared Playwright driver."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    _sessions: dict[str, LiveBrowser] = PrivateAttr(default_factory=dict)
    _playwright: Playwright | None = PrivateAttr(default=None)
    _lock: asyncio.Lock = PrivateAttr(default_factory=asyncio.Lock)

    async def create(self) -> LiveBrowser:
        async with self._lock:
            # ponytail: soft cap -- sessions are registered after start() below, so
            # rapid concurrent creates could overshoot by one; fine for a single-user
            # desktop tool where "New browser" clicks are sequential.
            if len(self._sessions) >= _MAX_SESSIONS:
                raise BrowserStartupError(
                    f"Too many open browsers ({len(self._sessions)}/{_MAX_SESSIONS}). "
                    "Close one before opening another."
                )
            if self._playwright is None:
                self._playwright = await async_playwright().start()
        session = LiveBrowser(session_id=uuid.uuid4().hex)
        started = False
        try:
            await session.start(self._playwright)
            started = True
        finally:
            if not started:
                # start() failed partway -- tear down so we don't leak a Chromium.
                await session.close()
        self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> LiveBrowser:
        # Dict access raises KeyError for a missing session; callers catch it.
        return self._sessions[session_id]

    async def close(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is not None:
            await session.close()

    async def shutdown(self) -> None:
        for session_id in list(self._sessions):
            await self.close(session_id)
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
