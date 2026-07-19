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
import queue
import shutil
import time
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Any, Literal

from browser_use import Agent, BrowserSession, ChatAnthropic
from browser_use.skill_cli.actions import ActionHandler
from imbue.imbue_common.mutable_model import MutableModel
from loguru import logger
from playwright.async_api import (
    Browser,
    BrowserContext,
    CDPSession,
    Page,
    Playwright,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError
from pydantic import PrivateAttr

from browser import manifest as fleet_manifest
from browser.names import generate_browser_name, is_valid_browser_name
from browser.oom_retag import notify_chromium_processes_expected

# browser-use phones home anonymized telemetry by default; disable it (the
# compute has no business making that call, and it spams connection-error logs
# where egress is restricted). setdefault so an explicit opt-in still wins.
os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")

# Errors expected when a page/target/CDP session goes away underneath us (tab
# closed, navigation, browser killed). PlaywrightError covers TargetClosedError.
_BROWSER_ERRORS = (RuntimeError, ConnectionError, OSError, PlaywrightError)

ControlOwner = Literal["human", "agent"]

# Explicit per-browser lifecycle. A browser is REGISTERED in the fleet the instant
# create() is called (so the viewer/CLI can address it at once), but its Chromium is
# launched asynchronously and serialized, so it starts in ``init`` and flips to
# ``running`` only once Chromium is up and the screencast is attached. ``crashed`` is
# terminal (Chromium died -- OOM/segfault). Driving/ownership only applies once
# ``running``; the viewer renders deterministically off this field.
Lifecycle = Literal["init", "running", "crashed"]

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

# Outbound buffer depth per cast WebSocket. Screencast frames are produced on the
# loop and drained by a Flask thread; if a client falls behind we drop the OLDEST
# frame (a stale frame is worthless -- only the latest matters) rather than block
# the loop. A handful of frames is plenty of slack for a momentarily-slow client.
_CAST_QUEUE_MAX_SIZE = 16

# Each live session = a headless Chromium + a Playwright observer; cap the concurrent
# count so a small compute (e.g. 4 GB) can't be OOM-ed. Override via BROWSER_MAX_SESSIONS.
_MAX_SESSIONS = int(os.environ.get("BROWSER_MAX_SESSIONS", "3"))

# Names whose background launch FAILED are remembered briefly so a late/retrying optimistic
# viewer (still in 1013 reconnect-backoff when the launch failed, so it never registered a
# cast queue and missed the launch_failed broadcast) is closed terminally (1008) instead of
# told "try again" forever. A small ring is plenty: the cap is 3, and an entry only needs to
# outlive a viewer's reconnect backoff (a few seconds). See BrowserSessionManager.
_FAILED_LAUNCH_MEMORY = int(os.environ.get("BROWSER_FAILED_LAUNCH_MEMORY", "32"))

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

# A human take-control is STICKY: it blocks agents until the human explicitly hands
# back ("Return to agent"). There is no idle/grace yield -- a human who grabs a
# browser keeps it even if they walk away mid-CAPTCHA/login, so they never come back
# to find an agent moved the page out from under them. (Agents still auto-release via
# _LEASE_IDLE_TTL; the asymmetry is deliberate -- a dead agent must not hoard, a human
# must not be force-yielded.)

# When the browser frees and is handed to a queued agent, that agent is *messaged*
# to resume (it ended its turn). If it doesn't actually take the wheel (send a
# command) within this window -- e.g. it was interrupted/killed -- the grant is
# revoked and the browser passes to the next waiter, instead of sitting idle for
# the full _LEASE_IDLE_TTL on a no-show.
_CLAIM_WINDOW = float(os.environ.get("BROWSER_CLAIM_WINDOW", "12"))

# Chromium's in-process sandbox cannot run as root: it exits with "Running as root
# without --no-sandbox is not supported" (crbug 638180), and browser-use swallows that
# into a ~30s launch hang. Every minds workspace runs this daemon as ROOT inside an OUTER
# boundary -- gVisor (runsc) under docker/cloud/AWS, the VM under Lima/Vultr -- so the
# inner sandbox is both unusable-as-root and redundant. We therefore disable it whenever
# we're root (the reliable signal; browser-use's own IN_DOCKER check misses the bare-VM
# Lima case, since Lima is a VM, not a container), and keep it for a non-root runtime
# (e.g. local dev) where it works and there may be no outer boundary. BROWSER_NO_SANDBOX=1
# forces it off regardless.
_NO_SANDBOX = os.environ.get("BROWSER_NO_SANDBOX", "").strip().lower() in ("1", "true", "yes", "on")


def _should_disable_sandbox() -> bool:
    """Whether to launch Chromium with its sandbox off: forced via BROWSER_NO_SANDBOX, or
    running as root (where Chromium refuses to start the sandbox). See _NO_SANDBOX."""
    return _NO_SANDBOX or os.geteuid() == 0


def _repo_root() -> Path:
    """The workspace root (where ``scripts/`` lives), anchored on this file's location
    rather than cwd -- used as the wake subprocess's cwd so the ``mngr`` dev shim
    resolves this checkout regardless of where the daemon was started."""
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "scripts").is_dir() and (candidate / "libs").is_dir():
            return candidate
    return Path.cwd()


# Where `screenshot` writes PNGs (relative to the daemon's cwd = repo root). The
# CLI prints the path and the agent reads the file; agent + daemon share the FS.
_SCREENSHOT_DIR = Path(os.environ.get("BROWSER_SCREENSHOT_DIR", "runtime/browser-screenshots"))

# Sentinel the fleet wraps its agent-facing nudges in before sending them via
# `mngr message` (see `_message_agent`). These nudges land in the agent's
# transcript as an ordinary user turn; without a marker the system_interface
# transcript UI shows them as a bare user bubble, as if the human had typed
# them. Wrapping lets that UI recognise the message and render it as a collapsed
# system chip instead (like Stop-hook feedback).
#
# CROSS-LAYER CONTRACT: the reading side is the frontend's `BROWSER_FLEET_TAG` in
# apps/system_interface/frontend/src/views/message-kinds.ts -- keep the tag in
# sync. We wrap here in the fleet's OWN service (not in mngr, which is an
# independent product with no stake in this display concern). The wrapper adds no
# newlines, so a wrapped message types into the agent's pane identically to the
# same text sent unwrapped.
_SYSTEM_MESSAGE_TAG = "agentic-browser-fleet"


def _wrap_system_message(text: str) -> str:
    """Wrap an automated agent-facing nudge in the ``_SYSTEM_MESSAGE_TAG`` sentinel
    (see its comment). Adds no newlines, so the wrapped text types into the agent's
    pane identically to ``text`` sent unwrapped."""
    return f"<{_SYSTEM_MESSAGE_TAG}>{text}</{_SYSTEM_MESSAGE_TAG}>"

# Per-browser persistent Chromium profiles (cookies/logins/history) live here, on the
# workspace volume under $MNGR_HOST_DIR -- Tier A durability: they survive stop/start
# and restart of a single workspace (lost only on a permanent delete). They are NOT
# under runtime/ (which the opt-in GitHub sync ships to a git branch) -- a fat,
# churny profile would bloat that branch. Override the root for tests / alternate layouts.
_PROFILE_ROOT = Path(
    os.environ.get(
        "BROWSER_PROFILE_ROOT",
        str(Path(os.environ.get("MNGR_HOST_DIR", "/mngr")) / "browser-profiles"),
    )
)
# Seconds to wait for one tab's navigation during restore, so a slow SSO redirect
# can't stall the sequential relaunch of the rest of the fleet.
_RESTORE_NAV_TIMEOUT = float(os.environ.get("BROWSER_RESTORE_NAV_TIMEOUT", "20"))
# How often the manager re-checkpoints the manifest (a no-op when nothing changed).
# Topology changes (create/close) checkpoint immediately; this catches tab-URL drift
# so an ungraceful daemon kill loses at most this many seconds of tab changes (the
# profile's cookies/logins persist regardless).
_MANIFEST_CHECKPOINT_SECONDS = float(os.environ.get("BROWSER_CHECKPOINT_SECONDS", "10"))
# Lock files Chromium leaves in a profile; a hard kill (crash/OOM/container stop)
# orphans them and the next launch on that profile would refuse to start. Safe to
# remove because restore is sequential and the prior Chromium for this dir is dead.
_SINGLETON_LOCK_NAMES = ("SingletonLock", "SingletonSocket", "SingletonCookie")


def _profile_dir(browser_id: str) -> Path:
    """The persistent Chromium ``user_data_dir`` for a browser name.

    The ``browser-use-user-data-dir-`` prefix in the final path component is
    LOAD-BEARING, not cosmetic: browser_use's ``BrowserProfile._copy_profile()``
    (profile.py) treats any other path as a "real" profile to COPY into a throwaway
    temp dir (because the bundled binary is "Google Chrome for Testing", so its
    is_chrome check is True) -- which would silently defeat persistence and recopy
    50-500MB on every launch. A path containing this substring hits its early-return
    and is used in place. Pinned by browser-use==0.13.1 and guarded by an integration
    test; do not rename without updating that test. Only the suffix changed from an
    int to the name string (validated filesystem-safe by names.is_valid_browser_name).
    """
    return _PROFILE_ROOT / f"browser-use-user-data-dir-{browser_id}"


def _clear_stale_singleton(profile_dir: Path) -> None:
    """Remove Chromium's Singleton* lock files left behind by a hard kill, so a
    relaunch on this persistent profile isn't refused. Called only at launch, never
    while a browser is live (one live Chromium per profile dir)."""
    for name in _SINGLETON_LOCK_NAMES:
        try:
            (profile_dir / name).unlink(missing_ok=True)
        except OSError as e:
            logger.debug("could not clear {} in {} ({})", name, profile_dir, e)


def _is_restorable_url(url: str | None) -> bool:
    """Whether a tab URL is worth persisting/reopening (skip blank and internal pages)."""
    return bool(url) and not url.startswith(("about:", "chrome:", "chrome-error:", "devtools:"))


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
    """Return ``(available, reason)`` for the optional, key-only ``task``/``extract``
    verbs. Direct control (state/click/input/scroll/...) is keyless and always
    available, so this never gates starting or driving a browser -- only those two
    verbs, which the daemon checks at call time."""
    if resolve_anthropic_key():
        return True, "Anthropic API key available"
    return (
        False,
        "The 'task' and 'extract' verbs need an Anthropic API key (create the workspace "
        "with the 'Anthropic API key' provider; the 'Claude subscription' option has no "
        "usable key). Direct control -- state/click/input/scroll/screenshot/tab -- works "
        "without one.",
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


class InvalidBrowserNameError(BrowserStartupError):
    """Raised when a user-typed browser name is syntactically invalid (maps to HTTP 400)."""


class DuplicateBrowserNameError(BrowserStartupError):
    """Raised when a user-typed name collides with a live browser (maps to HTTP 409).

    A crashed-but-not-closed browser still holds its name, so a duplicate can mean a
    dead shell is reserving it -- close that one to free the name (see the CLI/SKILL).
    """


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

    # The random ~2-word english NAME the user/agent sees (e.g. "alex-smith"). The
    # addressing key everywhere (CLI arg, cast WS path, manifest id, profile dir).
    # Stable and never reused: a closed name is gone, so a cached name is the same
    # browser or a 404. There is no default browser -- every name is created on demand.
    browser_id: str
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
    # Outbound fan-out queues, one per connected cast WebSocket. The WS lives on a
    # Flask thread (thread-per-connection); the loop pushes JSON frames onto its
    # queue and the Flask thread drains and sends them. queue.Queue is thread-safe
    # for the one-producer (loop) / one-consumer (Flask) handoff. The LIST itself is
    # mutated ONLY on the loop thread (register/unregister are awaited via the
    # bridge), so _broadcast can iterate it without a lock -- the single-loop
    # serialization is the guard. Mirrors apps/system_interface's WebSocketBroadcaster.
    _cast_queues: list["queue.Queue[str | None]"] = PrivateAttr(default_factory=list)
    _latest_frame: str | None = PrivateAttr(default=None)
    _send_in_flight: bool = PrivateAttr(default=False)
    _nav_tracked: set[Page] = PrivateAttr(default_factory=set)
    _active_target_id: str | None = PrivateAttr(default=None)
    _keepalive_task: "asyncio.Task[None] | None" = PrivateAttr(default=None)
    # The in-flight serialized launch task (set by the manager's _spawn_launch). close()
    # awaits it via the manager so a teardown can't race a suspended start() -- the launch
    # finishes/aborts first and observes _closed. None once create's launch isn't pending.
    _launch_task: "asyncio.Task[None] | None" = PrivateAttr(default=None)
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
    # Direct-control resume queue: agents whose command was rejected (a human or
    # another agent held the browser). They ended their turns; when the browser
    # frees they are handed it FIFO and messaged to resume (see _wake_agent). This
    # is separate from _wait_queue (the connection-bound blocking waiters used by
    # `task`/`hold`). ``(agent_id, agent_name)`` per entry, deduped by id.
    _resume_queue: list[tuple[str, str | None]] = PrivateAttr(default_factory=list)
    # When a resume-queue agent was handed the browser but hasn't sent a command
    # yet (the claim window); 0.0 once it claims (or when no grant is pending).
    _granted_at: float = PrivateAttr(default=0.0)
    # Strong refs to in-flight fire-and-forget tasks (the _wake_agent subprocess, the
    # crash announcement). asyncio keeps only weak references to bare create_task()
    # results, so without this they could be garbage-collected before they run.
    _bg_tasks: set[Any] = PrivateAttr(default_factory=set)
    # The single explicit lifecycle field (see ``Lifecycle``). A browser is registered
    # in ``init`` (Chromium not yet up), flips to ``running`` once Chromium is up and the
    # screencast is attached, and to ``crashed`` if Chromium dies unexpectedly (OS/OOM
    # kill, segfault) -- detected via the Playwright observer's `disconnected` event, or
    # lazily when an action finds the connection gone. A crashed browser reports
    # "crashed" to agents and the viewer rather than silently freezing; its name is never
    # reused (a new browser gets a new random name), so the dead one stays clearly
    # labeled until it is closed. All transitions stay on the single loop thread, so this
    # plain field needs no lock (cooperative single-thread atomicity).
    _lifecycle: Lifecycle = PrivateAttr(default="init")
    # Set by the manager: a no-arg hook that checkpoints the fleet manifest. Fired on
    # crash so a browser that died is dropped from the manifest promptly (not only on
    # the next ~10s checkpoint tick), so an ungraceful kill right after a crash doesn't
    # restore the dead browser as healthy next boot.
    _crash_save_hook: "Callable[[], None] | None" = PrivateAttr(default=None)

    @property
    def _crashed(self) -> bool:
        """Whether Chromium died unexpectedly. Backed by the single ``_lifecycle`` field
        (``crashed`` is terminal). A property -- not a separate flag -- so there is one
        source of truth; the setter exists so the crash-detection paths (and tests) can
        keep writing ``self._crashed = True`` while the real state lives in
        ``_lifecycle``."""
        return self._lifecycle == "crashed"

    @_crashed.setter
    def _crashed(self, value: bool) -> None:
        if value:
            self._lifecycle = "crashed"
        elif self._lifecycle == "crashed":
            self._lifecycle = "init"

    @property
    def _is_running(self) -> bool:
        """Chromium is up and the screencast attached -- the only state in which the
        browser can be driven and the viewer shows the live page."""
        return self._lifecycle == "running"

    def _build_bu_session(self, profile_dir: Path, chromium_path: str, *, chromium_sandbox: bool) -> BrowserSession:
        """Construct (don't start) the browser-use session for this browser's persistent
        profile. ``chromium_sandbox`` is False when Chromium's in-process sandbox must be
        disabled (see _NO_SANDBOX / the start() fallback); browser-use then injects
        ``--no-sandbox`` itself."""
        return BrowserSession(
            headless=_HEADLESS,
            executable_path=chromium_path,
            # Persistent profile on the workspace volume -- the whole point of
            # persistence. The dir name (see _profile_dir) is load-bearing for
            # browser_use. We deliberately do NOT set storage_state (it would
            # overwrite the live profile).
            user_data_dir=str(profile_dir),
            args=["--disable-dev-shm-usage"],
            chromium_sandbox=chromium_sandbox,
            keep_alive=True,
            # Pin a fixed viewport + window so every site renders at the same
            # resolution -- a consistent "Chromium in a small window", not a size
            # that shifts per page. Matches the screencast cap so frames never scale.
            viewport={"width": _SCREENCAST_MAX_WIDTH, "height": _SCREENCAST_MAX_HEIGHT},
            window_size={"width": _SCREENCAST_MAX_WIDTH, "height": _SCREENCAST_MAX_HEIGHT},
            device_scale_factor=1,
        )

    async def _start_bu_session(self, profile_dir: Path, chromium_path: str) -> BrowserSession:
        """Launch the browser-use session. The Chromium sandbox is disabled up front when
        we run as root or BROWSER_NO_SANDBOX is set (see _should_disable_sandbox) -- so on
        the bare-VM Lima case we never make the doomed sandboxed attempt that browser-use
        turns into a 30s hang. As a backstop, if a *sandboxed* launch still fails we retry
        once with the sandbox off (the only thing the retry changes), covering any non-root
        runtime that also can't sandbox."""
        disable_sandbox = _should_disable_sandbox()
        session = self._build_bu_session(profile_dir, chromium_path, chromium_sandbox=not disable_sandbox)
        try:
            await session.start()
        except (BrowserStartupError, *_BROWSER_ERRORS) as e:
            if disable_sandbox:  # sandbox was already off -> the failure is something else
                raise
            logger.warning(
                "browser {} failed to launch ({}); retrying without the Chromium sandbox", self.browser_id, e
            )
            _clear_stale_singleton(profile_dir)
            session = self._build_bu_session(profile_dir, chromium_path, chromium_sandbox=False)
            await session.start()
        return session

    async def start(
        self, playwright: Playwright, restore_tabs: list[str] | None = None, active_tab: int = 0
    ) -> None:
        """Launch the headless Chromium (browser-use) and attach the Playwright observer.

        Uses a persistent ``user_data_dir`` per browser name so cookies/logins/history
        survive a restart (Chromium's own persistence; we serialize none of it). When
        ``restore_tabs`` is given (a list of URLs from the manifest), reopen those tabs
        in order instead of the single default home page (and re-focus ``active_tab``);
        the persistent profile means they come back logged in.
        """
        self._playwright = playwright
        self._input_enabled.set()
        chromium_path = playwright.chromium.executable_path
        profile_dir = _profile_dir(self.browser_id)
        profile_dir.mkdir(parents=True, exist_ok=True)
        _clear_stale_singleton(profile_dir)  # a prior hard kill may have orphaned a lock
        self._bu_session = await self._start_bu_session(profile_dir, chromium_path)
        # The Chromium tree just spawned (and its processes self-write their
        # oom_score_adj moments later): have the OOM sweep re-band it.
        notify_chromium_processes_expected()
        # close() may have run while we were suspended in _start_bu_session (it holds no
        # lock and pops the browser before this resumes). If so, abort -- and kill the
        # Chromium we just brought up, so we don't leak a second handle behind a browser
        # that's already been torn down / removed.
        if await self._abort_start_if_torn_down():
            return
        cdp_url = self._bu_session.cdp_url
        if not cdp_url:
            raise BrowserStartupError("browser-use BrowserSession did not expose a cdp_url after start")
        observer = await playwright.chromium.connect_over_cdp(cdp_url)
        self._observer = observer
        # Detect an unexpected Chromium death (OS/OOM kill, segfault): the observer's
        # CDP connection drops and Playwright fires `disconnected`. Our own close()
        # also fires it, so the handler ignores the case where _closed is already set.
        observer.on("disconnected", self._on_disconnected)
        self._context = observer.contexts[0] if observer.contexts else await observer.new_context()
        self._context.on("page", self._on_new_page)
        pages = self._context.pages
        page = pages[0] if pages else await self._context.new_page()
        self._track_nav(page)
        await self._set_active_page(page)
        await self._open_initial_tabs(page, restore_tabs, active_tab)
        # Re-check ONE more time right before the terminal flip: a close() (or a crash
        # detected via the observer's disconnected event) may have landed during any of
        # the awaits above (connect_over_cdp / _set_active_page / _open_initial_tabs).
        # Without this we'd flip a torn-down / removed browser to "running" and broadcast
        # a stale live state. The observer is already attached here, so close()'s own
        # teardown covers the Chromium kill -- but if we got here via a launch that
        # close() didn't serialize against, kill it ourselves to be safe.
        if await self._abort_start_if_torn_down():
            return
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        # Chromium is up and the screencast is attached: flip init -> running and tell
        # every connected viewer, so an optimistic pane's "Starting browser…" overlay
        # comes down and the live canvas shows. Done last (after the screencast is live)
        # so a viewer that sees ``running`` is guaranteed real frames are coming.
        self._lifecycle = "running"
        self._broadcast(self._control_message())
        logger.info("LiveBrowser {} started (cdp_url={})", self.browser_id, cdp_url)

    async def _abort_start_if_torn_down(self) -> bool:
        """If close() or a crash landed while ``start`` was suspended at an await, abort the
        launch: kill the Chromium we already brought up (so a close()-during-launch can't
        leak a second handle) and report True so ``start`` returns without flipping to
        ``running``. Returns False (and does nothing) on the normal path. Idempotent and
        cheap; called at start()'s yield points after the bu_session exists."""
        if not (self._closed or self._crashed):
            return False
        bu_session = getattr(self, "_bu_session", None)
        if bu_session is not None:
            try:
                await bu_session.kill()
            except _BROWSER_ERRORS as e:
                logger.debug("aborted-launch kill ignored ({})", e)
        return True

    async def _open_initial_tabs(
        self, first_page: Page, restore_tabs: list[str] | None, active_tab: int = 0
    ) -> None:
        """Navigate the initial page(s): the saved tabs on restore, else the home page,
        then re-focus the tab that was active before the restart.

        Each navigation is bounded by ``_RESTORE_NAV_TIMEOUT`` so one slow/hung URL
        can't stall startup, and failures are swallowed (a tab that won't load just
        comes up blank -- the profile's cookies are already attached either way)."""
        # start() assigns _context immediately before calling this, so it is always set
        # here; assert it for the type checker and to explode loudly if that ever changes.
        context = self._context
        assert context is not None, "_open_initial_tabs called before the browser context was attached"
        urls = [u for u in (restore_tabs or []) if _is_restorable_url(u)] or [_HOME_URL]

        async def _go(page: Page, url: str) -> None:
            try:
                await asyncio.wait_for(page.goto(url), timeout=_RESTORE_NAV_TIMEOUT)
            except (TimeoutError, *_BROWSER_ERRORS) as e:
                logger.debug("restore nav to {} ignored ({})", url, e)

        pages = [first_page]
        await _go(first_page, urls[0])
        for url in urls[1:]:
            try:
                page = await context.new_page()
            except _BROWSER_ERRORS as e:
                logger.debug("restore new-tab for {} ignored ({})", url, e)
                continue
            self._track_nav(page)
            pages.append(page)
            await _go(page, url)
        # Re-focus the tab that was active before the restart (each new_page above
        # made itself active, so without this the LAST tab would be foregrounded).
        if 0 <= active_tab < len(pages) and pages[active_tab] is not self._active_page:
            await self._set_active_page(pages[active_tab])

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
            # Re-check after the await above: close() doesn't take _lock, so it can
            # null self._context while _stop_screencast() yields. Without this guard
            # new_cdp_session(page) would dereference None and the orphaned task's
            # AttributeError surfaces as "Task exception was never retrieved".
            if self._context is None:
                return  # torn down mid-teardown -- nothing to (re)attach to
            self._active_page = page
            try:
                cdp = await self._context.new_cdp_session(page)
                self._active_cdp = cdp
                try:
                    info = await cdp.send("Target.getTargetInfo")
                    self._active_target_id = info["targetInfo"]["targetId"]
                except _BROWSER_ERRORS:
                    self._active_target_id = None
                # Force a uniform render size on EVERY tab. browser-use pins the
                # viewport on the first page, but tabs opened later (by the agent or
                # by the site) can come up at a different size, so their frames would
                # stream at a different resolution and the viewer would letterbox them
                # inconsistently. Overriding the device metrics on each screencast
                # target makes every tab stream at exactly the screencast cap.
                try:
                    await cdp.send(
                        "Emulation.setDeviceMetricsOverride",
                        {
                            "width": _SCREENCAST_MAX_WIDTH,
                            "height": _SCREENCAST_MAX_HEIGHT,
                            "deviceScaleFactor": 1,
                            "mobile": False,
                        },
                    )
                except _BROWSER_ERRORS as e:
                    logger.debug("device-metrics override ignored ({})", e)
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
        # A new page usually means a new renderer process about to self-write
        # its oom_score_adj; have the OOM sweep re-band it.
        notify_chromium_processes_expected()
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
        # Any navigation (any frame, human- or agent-driven) can swap in a new
        # renderer process (site isolation); have the OOM sweep re-band it.
        notify_chromium_processes_expected()
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
                self._broadcast({"type": "frame", "data": frame})
        finally:
            self._send_in_flight = False

    async def _capture_one_frame(self) -> str | None:
        """Grab a single screencast-shaped JPEG (base64) of the active tab on demand and
        cache it as ``_latest_frame``.

        The CDP screencast only emits a frame on a REPAINT, so a browser that flipped to
        ``running`` and then sat on a static page has ``_latest_frame is None`` until it
        next changes -- a fresh viewer connecting in that window would see a black canvas
        (finding [6]). ``Page.captureScreenshot`` forces a one-off capture at the same
        format/quality the screencast uses, so the replayed frame is indistinguishable
        from a live one. Best-effort: returns None (and changes nothing) if there's no
        active CDP session or the capture fails."""
        cdp = self._active_cdp
        if cdp is None:
            return None
        try:
            shot = await cdp.send(
                "Page.captureScreenshot",
                {"format": _SCREENCAST_FORMAT, "quality": _SCREENCAST_QUALITY},
            )
        except _BROWSER_ERRORS as e:
            logger.debug("one-off frame capture ignored ({})", e)
            return None
        data = shot.get("data")
        if data:
            self._latest_frame = data
        return data

    async def _broadcast_tabs(self) -> None:
        # Stays async: it awaits _tab_list() (a CDP round-trip). The fan-out itself
        # (_broadcast) is now synchronous.
        self._broadcast({"type": "tabs", "tabs": await self._tab_list()})

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

    def tab_urls(self) -> tuple[list[str], int]:
        """The restorable tab URLs + the active tab's index within them, for the
        manifest. ``page.url`` is a cached property (no CDP round-trip), unlike the
        title fetch in ``_tab_list`` -- so the periodic checkpoint stays cheap."""
        if self._context is None:
            return [], 0
        urls: list[str] = []
        active = 0
        for page in self._context.pages:
            if _is_restorable_url(page.url):
                if page is self._active_page:
                    active = len(urls)
                urls.append(page.url)
        return urls, active

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
        doesn't let the WS proxy time out the idle stream; also sweep idle leases and
        refresh the viewer's idle-countdown / queue display while an agent holds."""
        while not self._closed:
            await asyncio.sleep(_KEEPALIVE_SECONDS)
            self._broadcast({"type": "ping"})
            if not self._is_running:
                continue  # init (no ownership yet) or crashed (dead): no sweeps/handoffs
            changed = await self._sweep_unclaimed_grant() or await self._sweep_idle_lease()
            if not changed and self.controller == "agent":
                self._broadcast(self._control_message())

    async def _sweep_idle_lease(self) -> bool:
        """Release a direct-control lease whose owner has gone quiet (dead/wandered-off
        agent). A running ``task`` (``_agent_task`` set) is connection-bound and exempt;
        the CAS keeps this from clobbering a freshly-handed-off lease. Returns True if it
        released one.

        Snapshot the control fields (controller / _agent_task / owner_agent_id /
        _lease_touched_at) under ``_control_lock`` so the idle check and the expect-tuple
        it builds are taken from one consistent view -- otherwise a concurrent ownership
        change between the reads could yield a torn expect-tuple. The CAS in
        ``_transition`` then re-validates against the live state before mutating.
        """
        async with self._control_lock:
            controller = self.controller
            agent_running = self._agent_task is not None
            owner_agent_id = self.owner_agent_id
            lease_touched_at = self._lease_touched_at
        if controller == "agent" and not agent_running and time.monotonic() - lease_touched_at > _LEASE_IDLE_TTL:
            return await self._transition(to="human", expect=("agent", owner_agent_id, False))
        return False

    async def _sweep_unclaimed_grant(self) -> bool:
        """A resume-queue agent was handed the browser and messaged to resume, but
        hasn't sent a command within ``_CLAIM_WINDOW`` (it was interrupted/killed, or
        never woke). Revoke the grant so the browser passes to the next waiter instead
        of sitting idle for the full idle-TTL on a no-show. ``_granted_at`` is set only
        for a pending grant and cleared the instant the agent sends its first command
        (``run_action``)."""
        async with self._control_lock:
            if (
                self.controller == "agent"
                and self._granted_at
                and self._agent_task is None
                and self._lease_touched_at < self._granted_at
                and time.monotonic() - self._granted_at > _CLAIM_WINDOW
            ):
                self._granted_at = 0.0
                await self._write_control_locked("human", None, None, pinned=False)
                await self._settle_queue_locked()
                return True
        return False

    def _human_pin_active(self) -> bool:
        """A human pin blocks agents until the human explicitly hands back
        (:meth:`return_to_agents`). Taking control is sticky on purpose -- a human can
        walk away mid-CAPTCHA/login and the browser is never yanked back. A *resting*
        human (controller=human, not pinned) is free: an agent takes it via
        :meth:`acquire`."""
        return self.controller == "human" and self.human_pinned

    # --- input ----------------------------------------------------------------

    async def handle_cast_message(self, message: dict[str, Any]) -> None:
        """Handle a message from a cast socket: human input or tab control.

        Input/tab/nav are gated on ``_input_enabled`` (set only while the human has
        control). The check and the CDP dispatch happen together under
        ``_control_lock`` so an agent acquiring the browser mid-dispatch can't let a
        stale human input land after the handoff (the input/control TOCTOU).
        """
        kind = message.get("type")
        if kind in ("mouse", "key", "tab", "navigate", "back", "forward", "reload"):
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
            elif kind == "back" and self._active_page is not None:
                await self._active_page.go_back()
            elif kind == "forward" and self._active_page is not None:
                await self._active_page.go_forward()
            elif kind == "reload" and self._active_page is not None:
                await self._active_page.reload()
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
        broadcasts the new state to every cast socket. The broadcast is now a plain
        synchronous fan-out (no ``await``), so the four-field write + broadcast run
        with no intervening yield -- the input gate can never be observed mid-write.
        Late joiners get the same state via ``register_cast_queue``'s initial seed.
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
        self._broadcast(self._control_message())

    def _waiting_names(self) -> list[str]:
        """Display names of every agent queued for this browser: the resume queue
        (agents auto-queued when their command was rejected) first, then any
        connection-bound task/hold waiters."""
        names = [name or agent_id for (agent_id, name) in self._resume_queue]
        names += [w.agent_name or w.agent_id for w in self._wait_queue]
        return names

    def _control_message(self) -> dict[str, Any]:
        msg: dict[str, Any] = {
            "type": "control",
            # The explicit lifecycle (init/running/crashed) the viewer renders off of:
            # init -> full "Starting browser…" overlay, running -> live page, crashed ->
            # crashed overlay. Carried on EVERY control broadcast so the viewer reacts
            # to each transition deterministically (not by guessing from frames).
            "lifecycle": self._lifecycle,
            "owner": self.controller,
            "owner_agent_id": self.owner_agent_id,
            "owner_name": self.owner_agent_name,
            "human_pinned": self.human_pinned,
            # Agents queued (monitor-and-wait) behind the current owner, in FIFO order.
            "waiting": self._waiting_names(),
        }
        # While an agent holds a sticky direct-control lease (not a connection-bound
        # task), tell the viewer how long it has been idle and when the idle-TTL will
        # auto-release it, so a watching human knows the browser will free itself.
        if self.controller == "agent" and self._agent_task is None and self._lease_touched_at:
            idle = time.monotonic() - self._lease_touched_at
            msg["idle_seconds"] = max(0, int(idle))
            msg["idle_release_seconds"] = max(0, int(_LEASE_IDLE_TTL - idle))
        return msg

    def _control_state(self) -> dict[str, Any]:
        """Owner snapshot embedded in every direct-command response so the agent can
        tell, after each call, whether it still holds control (e.g. a human took it).
        Carries the lifecycle too, so a caller acting on an ``init`` browser sees why
        the command was deferred."""
        return {
            "lifecycle": self._lifecycle,
            "controller": self.controller,
            "owner_agent_id": self.owner_agent_id,
            "owner_name": self.owner_agent_name,
            "human_pinned": self.human_pinned,
        }

    async def acquire_with_state(
        self,
        agent_id: str,
        agent_name: str | None = None,
        *,
        reclaim: bool = False,
        wait: bool = True,
        max_wait: float | None = None,
        enqueue_on_busy: bool = False,
    ) -> dict[str, Any]:
        """:meth:`acquire`, then snapshot the control state -- both ON the loop so the
        snapshot reflects the post-acquire ownership atomically.

        The runner's ``cmd_acquire`` reads ``_control_state()`` after acquiring; reading
        it on the Flask thread would observe loop-mutated fields without going through the
        bridge (a torn/stale view). Returning ``{ok, status, **control_state}`` from one
        coroutine keeps that read on the loop thread where every mutation also happens."""
        status = await self.acquire(
            agent_id, agent_name, reclaim=reclaim, wait=wait, max_wait=max_wait, enqueue_on_busy=enqueue_on_busy
        )
        return {
            "ok": status == "acquired",
            "status": status,
            # Only promise a resume in the CLI when the agent was actually enrolled (see run_action).
            "enqueued": enqueue_on_busy and status in ("busy_human", "busy_agent"),
            **self._control_state(),
        }

    async def handoff_with_state(self, agent_id: str, agent_name: str | None, reason: str) -> dict[str, Any]:
        """:meth:`handoff`, then snapshot the control state -- both ON the loop (see
        :meth:`acquire_with_state`), so the runner's ``cmd_handoff`` never reads
        loop-mutated ownership fields off the Flask thread."""
        handed = await self.handoff(agent_id, agent_name, reason)
        status = "handed_off" if handed else "not_owner"
        return {"ok": handed, "status": status, **self._control_state()}

    def _enqueue_resume_locked(self, agent_id: str, agent_name: str | None) -> None:
        """Add an agent to the resume queue (deduped by id). Caller holds _control_lock."""
        if not any(aid == agent_id for (aid, _) in self._resume_queue):
            self._resume_queue.append((agent_id, agent_name))

    def _enqueue_resume_front_locked(self, agent_id: str, agent_name: str | None) -> None:
        """Put an agent at the FRONT of the resume queue -- it handed off mid-task (e.g. a
        CAPTCHA), so it resumes before agents that were merely waiting their turn. Moves
        an existing entry to the front. Caller holds _control_lock."""
        self._resume_queue = [(aid, an) for (aid, an) in self._resume_queue if aid != agent_id]
        self._resume_queue.insert(0, (agent_id, agent_name))

    def _dequeue_resume_locked(self, agent_id: str) -> None:
        """Drop an agent from the resume queue (it took control / no longer waiting)."""
        self._resume_queue = [(aid, an) for (aid, an) in self._resume_queue if aid != agent_id]

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        """Run a fire-and-forget coroutine, holding a strong ref so it isn't GC'd."""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _spawn_wake(self, agent_id: str, agent_name: str | None) -> None:
        """Schedule a wake, holding a strong ref so the task isn't GC'd before it runs."""
        self._spawn(self._wake_agent(agent_id, agent_name))

    def _on_disconnected(self, _browser: Browser | None) -> None:
        """Playwright fires this when the Chromium CDP connection drops. During our own
        teardown (``_closed``) it's expected; otherwise the browser crashed -- record it
        and tell the viewer. The agent finds out on its next command (see run_action).

        The ``_browser`` argument is the Playwright callback's payload and is unused; it
        is typed ``| None`` because the lazy crash-detection path in ``run_action`` calls
        this with ``self._observer``, which may already be ``None`` when the connection
        is gone (and tests invoke it with ``None`` directly)."""
        if self._closed or self._crashed:
            return
        self._crashed = True
        self._spawn(self._announce_crash())

    async def _announce_crash(self) -> None:
        logger.warning("browser {} crashed (Chromium connection lost)", self.browser_id)
        self._broadcast({"type": "crashed", "browser_id": self.browser_id})
        # Release anyone queued for this browser: it will never free, so wait-queue waiters
        # must not hang and resume-queue agents must be told rather than wait for a wake
        # that never comes. (close() does the same for a user-closed browser.)
        async with self._control_lock:
            await self._abandon_queues_locked("crashed")
        if self._crash_save_hook is not None:
            # Drop the dead browser from the manifest now (it's excluded from the live
            # snapshot), so a kill right after the crash doesn't restore it as healthy.
            self._crash_save_hook()

    def _observer_alive(self) -> bool:
        """Whether the Chromium connection is still up (cheap, no round-trip)."""
        return self._observer is not None and self._observer.is_connected()

    def _crashed_payload(self) -> dict[str, Any]:
        return {"ok": False, "status": "crashed", **self._control_state()}

    def _starting_payload(self) -> dict[str, Any]:
        """Non-fatal "the browser is still launching" response for a command that arrives
        while the browser is still ``init`` (Chromium not up yet). The CLI maps this to
        the same wait-and-retry path as the fleet-still-restoring 503, so the agent waits
        rather than erroring out."""
        return {"ok": False, "status": "starting", **self._control_state()}

    async def _message_agent(self, agent_id: str, agent_name: str | None, text: str) -> None:
        """Best-effort: message a queued agent via ``mngr message`` (the same path
        launch-task uses). Failures are logged, not raised -- the claim window / lifecycle
        handling is the backstop if a message never lands.

        These are automated, non-human nudges, so the text is wrapped in the
        ``_SYSTEM_MESSAGE_TAG`` sentinel: the transcript UI recognises it and
        renders a collapsed system chip instead of a bare user bubble. This is
        display-only -- the agent still receives the message and resumes its turn
        exactly as before."""
        target = agent_name or agent_id
        wrapped = _wrap_system_message(text)
        try:
            proc = await asyncio.create_subprocess_exec(
                "mngr",
                "message",
                target,
                "--message",
                wrapped,
                # Run from the repo root so the `mngr` dev shim resolves this checkout
                # (repo-relative paths assume cwd = repo root; don't rely on the
                # daemon's inherited cwd).
                cwd=str(_repo_root()),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        except OSError as e:
            logger.warning("could not message agent {} for browser {} ({})", target, self.browser_id, e)

    async def _wake_agent(self, agent_id: str, agent_name: str | None) -> None:
        """Message a queued agent that the browser is its again, so it resumes in a
        fresh turn (it ended its turn when it lost control). If it fails, or the agent
        never shows, the claim window passes the browser on."""
        await self._message_agent(
            agent_id,
            agent_name,
            f"Browser {self.browser_id} was handed back to you (the human finished with it). "
            f"Re-run `state {self.browser_id}` to re-read the page, then continue where you left off.",
        )

    async def _abandon_queues_locked(self, reason: str) -> None:
        """The browser is gone (crashed or closed): release EVERY queued agent so none
        waits on a corpse. Caller holds ``_control_lock``.

        * ``_wait_queue`` (connection-bound task/hold waiters) are woken ungranted -> their
          ``acquire`` falls through to the crashed/closed check and returns that status, so
          the streaming endpoint ends with a clear "gone" instead of hanging forever on a
          browser that will never free.
        * ``_resume_queue`` agents ended their turn waiting to be MESSAGED when it frees;
          it never will, so message each that it's gone and clear the queue -- otherwise
          they wait forever for a wake that never comes.
        """
        waiters, self._wait_queue = self._wait_queue, []
        for waiter in waiters:
            waiter.granted = False
            waiter.event.set()
        resume, self._resume_queue = self._resume_queue, []
        for agent_id, agent_name in resume:
            self._spawn(
                self._message_agent(
                    agent_id,
                    agent_name,
                    f"Browser {self.browser_id} is gone ({reason}) and won't come back. "
                    f"Start a new browser with `new` if you still need one.",
                )
            )

    async def _settle_queue_locked(self) -> None:
        """Reconcile both wait-queues with the current control state. Holds ``_control_lock``.

        * human-pinned -> evict the connection-bound ``_wait_queue`` (task/hold waiters
          never block on a human); the resume queue PERSISTS -- those agents want the
          browser back *after* the human is done.
        * free (unpinned human) -> hand the browser to the first waiter: a live
          ``_wait_queue`` waiter if any, else the first ``_resume_queue`` agent, which
          is messaged to resume (it ended its turn) and put on the claim clock.
        * agent-owned -> nothing (someone holds it; queues stay put).
        """
        if self.controller == "human" and self.human_pinned:
            waiters, self._wait_queue = self._wait_queue, []
            for waiter in waiters:
                waiter.granted = False
                waiter.event.set()
            return
        if self.controller != "human":
            return
        if self._wait_queue:
            waiter = self._wait_queue.pop(0)
            # An agent can be in BOTH queues (it sent a direct command -> resume queue,
            # then ran `task`/`acquire --wait` -> wait queue). Granting it here must
            # also clear its resume-queue entry, or a later settle would re-grant the
            # freed browser to an agent that's already done and spuriously wake it.
            self._dequeue_resume_locked(waiter.agent_id)
            await self._write_control_locked("agent", waiter.agent_id, waiter.agent_name, pinned=False)
            waiter.granted = True
            waiter.event.set()
        elif self._resume_queue:
            agent_id, agent_name = self._resume_queue.pop(0)
            await self._write_control_locked("agent", agent_id, agent_name, pinned=False)
            self._granted_at = time.monotonic()  # start the claim window
            self._spawn_wake(agent_id, agent_name)

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
                # A human taking control of a browser an agent is DRIVING queues that agent
                # at the FRONT of the resume queue, so it resumes first when the human hands
                # back -- regardless of what it runs next. Without this, a preempted agent
                # whose next command is the read-only `state` re-check (which deliberately
                # does NOT enrol a waiter) would be silently dropped: told "you're queued"
                # while in no queue, never woken on hand-back, and not shown in the human's
                # waiting list (so the "Return control to agents" button never appears).
                # Mirrors the agent-initiated handoff; the human-pinned settle below keeps
                # the resume queue intact.
                if self.controller == "agent" and self.owner_agent_id is not None:
                    self._enqueue_resume_front_locked(self.owner_agent_id, self.owner_agent_name)
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
        enqueue_on_busy: bool = False,
        on_wait: Callable[[str | None, str | None], Awaitable[None]] | None = None,
    ) -> str:
        """Acquire control for an agent. Returns one of:

        ``"acquired"`` -- the agent now controls the browser.
        ``"busy_human"`` -- a human took control (pinned); it stays the human's until
            they hand back. Only an explicit ``reclaim`` takes it. A *resting* human
            (not pinned) is free and taken.
        ``"busy_agent"`` -- another agent holds it and ``wait`` was False.
        ``"timed_out"`` -- waited ``max_wait`` seconds for another agent to release.
        ``"starting"`` -- the browser is still launching (``init``); driving/ownership
            only applies once running. Non-fatal -- the caller waits and retries.
        ``"crashed"`` -- Chromium died; the browser is gone.

        With ``wait`` (the default) and another agent in control, the caller parks in
        a FIFO queue and is handed the browser the instant that agent releases.

        With ``enqueue_on_busy`` (the direct-control path), a ``busy_human`` /
        ``busy_agent`` result also adds the agent to the resume queue: it ended its
        turn, and the daemon will message it to resume when the browser frees.
        """
        # Ownership/driving only applies once the browser is running. An init browser
        # has no Chromium yet (and no _bu_session to drive); a crashed one is gone. Both
        # are reported here so task/hold/acquire don't park a waiter on (or try to drive)
        # a browser that can't be driven. run_action gates on lifecycle before it calls
        # acquire, so this is the guard for the task/hold/explicit-acquire paths.
        if self._crashed:
            return "crashed"
        if not self._is_running:
            return "starting"
        async with self._control_lock:
            if self.controller == "agent" and self.owner_agent_id == agent_id:
                self.owner_agent_name = agent_name  # refresh display name on re-acquire
                self._dequeue_resume_locked(agent_id)
                return "acquired"
            # ``reclaim`` deliberately overrides a human pin for ANY agent, not just the
            # displaced owner: it is the "the human told me to keep going / take over" verb,
            # and the daemon cannot verify which agent the human addressed. This is an
            # intentional trust assumption (cooperative agents following the skill, which
            # says to reclaim ONLY on an explicit user instruction), not an oversight -- the
            # human's own take-control always wins again instantly if they disagree.
            if not reclaim and self._human_pin_active():
                if enqueue_on_busy:
                    self._enqueue_resume_locked(agent_id, agent_name)
                    self._broadcast(self._control_message())
                return "busy_human"
            if self.controller == "human":  # free, a stale pin, or reclaim of a pin
                self._dequeue_resume_locked(agent_id)
                await self._write_control_locked("agent", agent_id, agent_name, pinned=False)
                return "acquired"
            # controller == "agent", a different agent -> must wait or fail fast.
            if not wait:
                if enqueue_on_busy:
                    self._enqueue_resume_locked(agent_id, agent_name)
                    self._broadcast(self._control_message())
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
        # The browser may have died while we were parked: crash/close evicts the wait queue
        # ungranted, so report that (not a misleading "busy_human") and the agent starts fresh.
        if self._crashed:
            return "crashed"
        if self._closed:
            return "closed"
        return "acquired" if waiter.granted else "busy_human"

    async def release(self, agent_id: str) -> bool:
        """Release this agent's control back to the human (free). CAS: only the owner can."""
        return await self._transition(to="human", expect=("agent", agent_id, False))

    async def take_control(self) -> bool:
        """Human 'take control': preempt whatever agent is driving and pin (agents locked out).

        Always wins (no ``expect``): flips to a pinned human and cancels the run. The
        cancel happens outside the control lock, so the run's finally can re-enter the
        state machine without deadlocking. The pin is sticky -- it holds until the human
        explicitly hands back via :meth:`return_to_agents`, with no idle/grace yield (a
        human who took control keeps it even if they step away).

        Gated on lifecycle (like :meth:`acquire` / :meth:`run_action`): ownership only
        applies once the browser is ``running``. Taking control of an ``init`` browser
        would pin it before Chromium is even up, so it would come up locked to the human
        and block every agent; a ``crashed`` browser is gone. In both cases this no-ops
        (returns False) -- the human can take control once it's live. Returns True when
        the pin landed.
        """
        if not self._is_running:
            return False
        return await self._transition(to="human", pinned=True, preempt=True)

    async def handoff(self, agent_id: str, agent_name: str | None, reason: str) -> bool:
        """Agent-initiated handoff to the human (e.g. a CAPTCHA / verification it can't
        solve). Atomically, if the caller currently holds the browser: put it at the
        FRONT of the resume queue (it's mid-task), then hand control to the human PINNED.
        Control goes to the *human* -- not the next queued agent -- and stays there until
        the human explicitly returns it (the sticky pin), at which point this requester is
        the first agent woken to resume. Returns False (no change) if the caller doesn't
        hold it (a human already took over, or its lease lapsed).
        """
        async with self._control_lock:
            if not (self.controller == "agent" and self.owner_agent_id == agent_id):
                return False
            self._enqueue_resume_front_locked(agent_id, agent_name)
            await self._write_control_locked("human", None, None, pinned=True)
            await self._settle_queue_locked()  # evict any connection-bound task/hold waiters
            self._broadcast(
                {
                    "type": "handoff_request",
                    "browser_id": self.browser_id,
                    "agent_name": agent_name or agent_id,
                    "reason": reason,
                    "url": self._active_page.url if self._active_page is not None else None,
                    **self._control_state(),
                }
            )
        return True

    async def return_to_agents(self) -> bool:
        """Human hands control back: un-pin (only if currently pinned). Frees any waiter."""
        return await self._transition(to="human", pinned=False, expect=("human", None, True))

    async def run_agent(self, agent_id: str, prompt: str, on_event: EventSink) -> None:
        """Run a browser-use task against this (already-acquired) browser, streaming steps.

        The caller (the task endpoint) acquires the browser in one submitted coroutine
        and submits this run as a SEPARATE coroutine; between the two the loop is free to
        run a human ``take_control`` (or an idle-lease sweep). So registering this run's
        cancellable handle (``_agent_task``/``_agent``) MUST be atomic with ownership:
        we take ``_control_lock`` and re-check that ``agent_id`` still owns the browser
        (``controller == "agent"`` and ``owner_agent_id == agent_id``, unpinned) BEFORE
        registering the handle and driving. This mirrors the pre-refactor design, where
        acquire and the ``run_agent`` task lived in one coroutine on the loop with no
        intervening preemption -- the invariant being that the cancellable handle and
        ownership move together.

        If ownership was lost in that gap (a human preempted, or the lease was swept),
        we emit ``lost_control`` and return WITHOUT touching the browser -- we never
        drive a browser the human (or another agent) now owns. Once the handle is
        registered under the lock, a subsequent ``take_control`` sees ``_agent_task``
        and cancels this run via the bridge.
        """
        api_key = resolve_anthropic_key()
        if not api_key:
            await on_event({"type": "error", "text": anthropic_key_status()[1]})
            return
        # Key is passed straight to ChatAnthropic -- never into os.environ, which would
        # leak across the manager's concurrent sessions and race between runs. Build the
        # Agent BEFORE taking the lock (it mutates no shared state); only the handle
        # registration below must be atomic with the ownership re-check.
        agent = Agent(
            task=prompt,
            llm=ChatAnthropic(model=_DEFAULT_MODEL, api_key=api_key),
            browser_session=self._bu_session,
        )
        async with self._control_lock:
            if self._state_tuple() != ("agent", agent_id, False):
                # A human took control (or the lease was swept) between the caller's
                # acquire and this run starting. Do not register the handle or drive --
                # the browser is no longer ours.
                await on_event({"type": "lost_control", **self._control_state()})
                return
            self._run_on_event = on_event
            self._agent_task = asyncio.current_task()
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
        self,
        agent_id: str,
        agent_name: str | None,
        action: Callable[[ActionHandler], Awaitable[dict[str, Any]]],
        enqueue_on_busy: bool = True,
    ) -> dict[str, Any]:
        """Run one direct-control action for an agent, returning a result + owner snapshot.

        ``enqueue_on_busy`` (default True) queues the agent to resume when a busy browser
        frees. The read-only ``state`` passes False: merely *looking* at a browser a
        human/another agent is driving must not silently enrol the agent as a waiter.

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
        # The browser died (OS/OOM kill, crash): don't try to acquire or drive a
        # corpse -- tell the agent it's gone so it starts a fresh one.
        if self._crashed:
            return self._crashed_payload()
        # Still launching (registered but Chromium not up yet): driving/ownership only
        # applies once running. Return a clear, non-fatal "starting" so the CLI/agent
        # waits and retries instead of erroring on a half-built browser.
        if not self._is_running:
            return self._starting_payload()
        # Did I already hold the lease, or does this command newly take the browser?
        # The client uses this to surface the browser pane exactly once -- on the
        # first command for a browser (and again after a human hands it back) --
        # rather than on every click.
        was_mine = self._state_tuple() == ("agent", agent_id, False)
        status = await self.acquire(agent_id, agent_name, wait=False, enqueue_on_busy=enqueue_on_busy)
        if status != "acquired":
            # ``enqueued`` tells the CLI whether the agent was actually enrolled to be woken
            # so it only promises "you're queued ... messaged when it frees" when true. The
            # read-only `state` peek passes enqueue_on_busy=False, so a busy `state` must NOT
            # over-promise a resume that will never come.
            return {
                "ok": False,
                "status": status,
                "enqueued": enqueue_on_busy and status in ("busy_human", "busy_agent"),
                **self._control_state(),
            }
        async with self._control_lock:
            if self._state_tuple() != ("agent", agent_id, False):
                # A human grabbed control in the tiny window between acquire and here.
                # Queue this agent to resume (same as the busy_human path) so the
                # daemon messages it back when the human hands the browser over -- but
                # only for state-changing commands, not a passive `state` peek.
                if enqueue_on_busy:
                    self._enqueue_resume_locked(agent_id, agent_name)
                    self._broadcast(self._control_message())
                return {"ok": False, "status": "lost_control", "enqueued": enqueue_on_busy, **self._control_state()}
            self._lease_touched_at = time.monotonic()
            self._granted_at = 0.0  # the agent claimed (sent a command); cancel the claim window
        async with self._lock:
            if self._context is None:
                return {"ok": False, "status": "closed", **self._control_state()}
            try:
                result = await action(self._ensure_action_handler())
            except _BROWSER_ERRORS as e:
                logger.debug("direct action failed on browser {} ({})", self.browser_id, e)
                # If the connection is gone, the browser crashed (the `disconnected`
                # event may not have fired yet) -- classify it so the agent gets a
                # clear "crashed, start a new one" rather than a raw CDP exception.
                if not self._observer_alive():
                    self._on_disconnected(self._observer)  # idempotent: marks + announces once
                    return self._crashed_payload()
                return {"ok": False, "status": "error", "error": str(e), **self._control_state()}
        return {"ok": True, "status": "ok", "newly_acquired": not was_mine, **result, **self._control_state()}

    def _node(self, index: int) -> Any:
        """Resolve an element index from the last ``state`` snapshot to its DOM node."""
        return self._selector_map.get(index)

    async def act_state(self, agent_id: str, agent_name: str | None) -> dict[str, Any]:
        async def _do(handler: ActionHandler) -> dict[str, Any]:
            summary = await handler.get_state()
            self._selector_map = dict(getattr(summary.dom_state, "selector_map", {}) or {})
            elements = summary.dom_state.llm_representation()
            return {"url": summary.url, "title": summary.title, "elements": elements, "tabs": await self._tab_list()}

        # state is a read-only peek: don't enqueue the agent as a waiter on a busy browser.
        return await self.run_action(agent_id, agent_name, _do, enqueue_on_busy=False)

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

    async def register_cast_queue(self) -> "queue.Queue[str | None]":
        """Register a new cast WebSocket and SEED its initial sync, atomically on the loop.

        Returns an outbound queue for the Flask cast handler to drain. The initial
        control + tabs (+ crash) sync is pushed BEFORE the queue is added to the
        fan-out list, so the viewer's first messages are deterministic -- no live
        frame can interleave ahead of the control/tabs the viewer needs first.

        We also replay a screencast frame (``_latest_frame``) to a new client of a
        RUNNING browser. The CDP screencast only emits a frame on a repaint, so a client
        connecting to a browser sitting on a static/blank page would otherwise see a
        black canvas (and the viewer's "Starting browser…" banner would never clear)
        until the page next changed. If no frame has been cached yet (the common case for
        a browser that just flipped ``init`` -> ``running`` and hasn't repainted --
        finding [6]), we force a one-off capture so even the very first viewer sees the
        live page at once. Skipped when crashed -- a dead browser shows the crash state,
        not a stale frame.

        The seed is at most four messages onto a fresh, empty queue whose maxsize
        (``_CAST_QUEUE_MAX_SIZE`` = 16) is far larger, so the ``put_nowait``s here can
        never raise ``queue.Full`` -- but the late-frame push goes through the same
        Full-safe ``_broadcast``-style path for symmetry (finding [8]).

        Runs on the loop (the runner calls it via ``bridge.run``), so the list
        mutation is single-threaded with respect to :meth:`_broadcast`.
        """
        client_queue: "queue.Queue[str | None]" = queue.Queue(maxsize=_CAST_QUEUE_MAX_SIZE)
        # The control message carries the lifecycle, so the viewer's FIRST message tells
        # it whether to show the init overlay / live page / crashed overlay -- no guessing
        # from frames. tabs follow (empty until running).
        client_queue.put_nowait(json.dumps(self._control_message(), default=str))
        client_queue.put_nowait(json.dumps({"type": "tabs", "tabs": await self._tab_list()}, default=str))
        if self._crashed:  # a viewer opening a crashed browser sees the crash state at once
            client_queue.put_nowait(json.dumps({"type": "crashed", "browser_id": self.browser_id}, default=str))
        elif self._is_running:
            # Replay the live page so a new client isn't stuck on black. Capture one on
            # demand if nothing has been cached yet (just flipped to running, no repaint).
            frame = self._latest_frame if self._latest_frame is not None else await self._capture_one_frame()
            if frame is not None:
                client_queue.put_nowait(json.dumps({"type": "frame", "data": frame}, default=str))
        self._cast_queues.append(client_queue)
        return client_queue

    async def register_cast_queue_with_lifecycle(self) -> "tuple[queue.Queue[str | None], Lifecycle]":
        """:meth:`register_cast_queue`, returning the new queue AND the browser's lifecycle
        captured ON the loop in the same step.

        The runner uses the lifecycle to decide whether to push the fleet-level
        ``initializing`` banner: a viewer that joins an already-``running`` browser must
        NOT be told it's initializing (finding [3-runner]), even while the whole fleet is
        still restoring -- the seeded ``control`` already carries ``lifecycle=running`` and
        the live page is right there. Reading the lifecycle here (not on the Flask thread)
        keeps it consistent with the seed that was just built."""
        client_queue = await self.register_cast_queue()
        return client_queue, self._lifecycle

    async def unregister_cast_queue(self, client_queue: "queue.Queue[str | None]") -> None:
        """Remove a cast queue from the fan-out. Async so it runs ON the loop (via
        ``bridge.run``), keeping all ``_cast_queues`` list mutation single-threaded with
        respect to :meth:`_broadcast` -- no lock needed because the loop serializes it."""
        if client_queue in self._cast_queues:
            self._cast_queues.remove(client_queue)

    async def describe(self) -> dict[str, Any]:
        """Snapshot for ``GET /browsers``: id, lifecycle, owner, and the tab list.

        ``lifecycle`` (init/running/crashed) is the explicit state the whole system
        reads; ``crashed`` is kept as a derived convenience for existing consumers (the
        CLI ``ls`` owner label). A browser still in ``init`` has no Chromium yet, so its
        tab list is empty (the round-trip would have nothing to read)."""
        return {
            "id": self.browser_id,
            "lifecycle": self._lifecycle,
            "controller": self.controller,
            "owner_agent_id": self.owner_agent_id,
            "owner_name": self.owner_agent_name,
            "human_pinned": self.human_pinned,
            "waiting": self._waiting_names(),
            "crashed": self._crashed,
            "tabs": [] if not self._is_running else await self._tab_list(),
        }

    def _broadcast(self, message: dict[str, Any]) -> None:
        """Fan a message out to every connected cast socket's outbound queue.

        Runs on the loop thread; pushes a JSON string onto each per-socket
        ``queue.Queue`` (thread-safe) for the owning Flask thread to send. On a
        full queue the client is behind, so we drop the OLDEST buffered frame and
        enqueue this one (a stale frame is worthless -- only the latest matters),
        mirroring WebSocketBroadcaster's drop-oldest policy.

        This is a plain ``def`` (no ``await``): it used to ``await ws.send_json``
        -- a real suspension point inside ``_write_control_locked`` while holding
        ``_control_lock`` -- and now only enqueues, which TIGHTENS the state
        machine's atomicity (one fewer mid-write yield). All call sites call it
        synchronously (no ``await``). The ``_cast_queues`` list is mutated only on
        this same loop thread (register/unregister go through the bridge), so
        iterating it here needs no lock.
        """
        text = json.dumps(message, default=str)
        for client_queue in self._cast_queues:
            try:
                client_queue.put_nowait(text)
            except queue.Full:
                try:
                    client_queue.get_nowait()  # drop the oldest buffered frame
                    client_queue.put_nowait(text)
                except (queue.Empty, queue.Full):
                    pass  # a concurrent drain raced us; the client will catch up

    def _shutdown_cast_queues(self) -> None:
        """Push the ``None`` shutdown sentinel onto every connected cast queue so each
        Flask cast thread tears down deterministically on the NEXT drain, instead of only
        when the client happens to disconnect. Runs on the loop (same as ``_broadcast``),
        so iterating ``_cast_queues`` needs no lock. Best-effort per queue: a full queue
        is drained once to make room for the sentinel (the client is going away anyway)."""
        for client_queue in self._cast_queues:
            try:
                client_queue.put_nowait(None)
            except queue.Full:
                try:
                    client_queue.get_nowait()  # make room; the client is shutting down
                    client_queue.put_nowait(None)
                except (queue.Empty, queue.Full):
                    pass

    async def close(self) -> None:
        self._closed = True
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
        # Tell every cast socket to tear down (don't wait for the client to disconnect).
        self._shutdown_cast_queues()
        # Release every queued agent so none hangs on a browser being torn down: wait-queue
        # waiters unblock (their acquire returns `closed`); resume-queue agents are messaged
        # it's gone and cleared.
        async with self._control_lock:
            await self._abandon_queues_locked("closed")
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
    Every browser is created on demand with a random ~2-word english NAME -- there
    is no default browser and the fleet starts EMPTY. Names are unique within the
    live fleet (generated under :attr:`_lock`, regenerated on collision) and never
    reused: a closed name is gone.

    REGISTER-INIT-IMMEDIATELY (the responsiveness fix): :meth:`create` registers a new
    :class:`LiveBrowser` in ``init`` under :attr:`_lock` (cap check + name resolution +
    add to ``_browsers``) and RETURNS at once, kicking the multi-second Chromium launch
    off as a background task. The route no longer blocks on the launch, so the
    optimistic viewer pane finds a real browser the instant it connects (the 1013
    "not-registered-yet" window shrinks to the sub-millisecond gap before the dict insert
    is visible).

    SERIALIZATION INVARIANT (the OOM guard): at most one Chromium is ``start()``-ing at a
    time. This is enforced by :attr:`_startup_lock` (a dedicated asyncio.Lock), which the
    background launch (:meth:`_launch`) holds across the WHOLE (multi-second) launch.
    Multiple ``init`` browsers queue on it and boot back-to-back, never in parallel.
    Registration (under :attr:`_lock`) is decoupled from launching (under
    :attr:`_startup_lock`): the cap counts ``init`` browsers too, so a flood of creates
    can't overshoot even though their launches run later. ``init`` browsers DO count
    toward the cap -- a half-started fleet still reserves its slots.
    """

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    _browsers: dict[str, LiveBrowser] = PrivateAttr(default_factory=dict)
    _playwright: Playwright | None = PrivateAttr(default=None)
    _lock: asyncio.Lock = PrivateAttr(default_factory=asyncio.Lock)
    # Serializes the actual Chromium launches (the OOM guard). Decoupled from ``_lock``
    # (which serializes registry mutation): registration is instant, launching is slow,
    # so they take different locks. At most one launch runs at a time; ``init`` browsers
    # queue here and boot one after another. Strong refs to the in-flight launch tasks so
    # asyncio doesn't GC a bare create_task() result before it runs.
    _startup_lock: asyncio.Lock = PrivateAttr(default_factory=asyncio.Lock)
    _launch_tasks: set[Any] = PrivateAttr(default_factory=set)
    # Last manifest JSON written, so the periodic checkpoint is a no-op when nothing
    # changed (idle workspaces produce zero backup-branch churn).
    _last_manifest_json: str | None = PrivateAttr(default=None)
    _closed: bool = PrivateAttr(default=False)
    _checkpoint_task: "asyncio.Task[None] | None" = PrivateAttr(default=None)
    _bg_save_tasks: set[Any] = PrivateAttr(default_factory=set)  # strong refs for _spawn_save
    # Bounded ring of names whose background launch FAILED (finding [7]). A late/retrying
    # optimistic viewer that was in 1013 reconnect-backoff when the launch failed never
    # registered a cast queue, so it missed the launch_failed broadcast; the cast handler
    # consults this so such a name is closed 1008 (terminal) instead of looping on 1013.
    # ``deque(maxlen=...)`` auto-evicts the oldest, so this can't grow unbounded; mutated
    # only on the loop thread (the launch task + the cast resolve), so it needs no lock.
    _failed_launch_names: "deque[str]" = PrivateAttr(default_factory=lambda: deque(maxlen=_FAILED_LAUNCH_MEMORY))

    def _register_init_locked(self, name: str) -> LiveBrowser:
        """Construct a LiveBrowser in ``init`` and add it to the registry. Caller must
        hold ``self._lock``, so the cap check + name resolution + insert are atomic (no
        cap overshoot, no duplicate-name TOCTOU). Does NOT launch Chromium -- the caller
        kicks :meth:`_launch` off as a background task after releasing the lock."""
        session = LiveBrowser(browser_id=name)
        session._crash_save_hook = self._spawn_save  # checkpoint promptly if it crashes
        self._browsers[name] = session
        # A fresh registration supersedes any earlier launch-failure for this name (the
        # user re-created it, or restore is retrying it), so it's no longer terminal for a
        # viewer -- drop it from the failed ring so the cast handler stops 1008-ing it.
        self._clear_failed_launch(name)
        return session

    def _clear_failed_launch(self, name: str) -> None:
        """Forget a name's prior launch failure (it's being (re)launched). Mutated only on
        the loop thread, so no lock is needed."""
        if name in self._failed_launch_names:
            self._failed_launch_names = deque(
                (n for n in self._failed_launch_names if n != name), maxlen=_FAILED_LAUNCH_MEMORY
            )

    def recently_failed_launch(self, name: str) -> bool:
        """Whether ``name``'s last background launch failed (and it has not since been
        re-registered). The cast handler uses this to close a stale optimistic viewer
        terminally (1008) rather than telling it to retry (1013) forever (finding [7])."""
        return name in self._failed_launch_names

    async def recently_failed_launch_async(self, name: str) -> bool:
        """``recently_failed_launch`` for the cast handler to reach via ``bridge.run`` --
        running the ``_failed_launch_names`` read ON the loop thread (where the launch task
        mutates it) is what makes it race-free, like ``capacity_async``."""
        return self.recently_failed_launch(name)

    async def _launch(
        self, session: LiveBrowser, restore_tabs: list[str] | None = None, active_tab: int = 0, persist: bool = True
    ) -> None:
        """Serialized background Chromium launch for an already-registered ``init``
        browser. Holds :attr:`_startup_lock` across the WHOLE launch, so at most one
        Chromium starts at a time (the OOM guard) -- multiple ``init`` browsers queue here
        and boot back-to-back. On success ``session.start`` flips the lifecycle to
        ``running`` and broadcasts; on failure (Chromium never came up) we REMOVE the
        browser from the registry rather than leaving a stranded ``init`` shell -- an
        init that never launched would otherwise keep its name reserved and its cap slot
        forever, and (unlike a crash, which preserves a dead shell that the user explicitly
        closes) there is nothing for the user to look at. Runs entirely on the loop, so the
        registry mutation needs no extra lock.

        ``persist`` (default True for ``create``): checkpoint the manifest once the browser
        is running, since a new running browser is a topology change. Restore passes
        ``persist=False`` -- the post-restore reconcile owns the manifest there, and a
        per-launch save would race it and clobber the preserved-for-retry entries of
        browsers that flaked this boot.

        Playwright is started lazily here under ``_startup_lock``. On an empty-fleet fresh
        workspace this means Playwright is first started by the FIRST launch (restore
        launches nothing), not pre-warmed by restore -- harmless (same loop, no deadlock),
        just noted so the timing isn't surprising."""
        async with self._startup_lock:
            if session._closed or session.browser_id not in self._browsers:
                return  # closed (or already removed) while it sat in the launch queue
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            try:
                await session.start(self._playwright, restore_tabs=restore_tabs, active_tab=active_tab)
            except (BrowserStartupError, *_BROWSER_ERRORS) as e:
                logger.warning("browser {} failed to launch ({}); removing it", session.browser_id, e)
                self._browsers.pop(session.browser_id, None)
                # Remember the name as launch-failed (finding [7]) so a late/retrying
                # optimistic viewer -- one still in 1013 reconnect-backoff when this failed,
                # which never registered a cast queue and so missed the launch_failed
                # broadcast below -- is closed terminally (1008) instead of looping on 1013.
                self._failed_launch_names.append(session.browser_id)
                # Tell any viewer waiting on the optimistic pane that this name is gone
                # (terminal) BEFORE close() pushes the shutdown sentinel onto the cast
                # queues -- so the viewer sees the launch_failed message and then the
                # socket tears down deterministically, not only on its own disconnect.
                session._broadcast({"type": "launch_failed", "browser_id": session.browser_id})
                await session.close()  # don't leak a half-started Chromium; pushes the sentinel
                return
        # A new RUNNING browser is a topology change worth persisting promptly (create);
        # restore defers to its reconcile instead (persist=False).
        if persist:
            self._spawn_save()

    def _spawn_launch(
        self, session: LiveBrowser, restore_tabs: list[str] | None = None, active_tab: int = 0
    ) -> "asyncio.Task[None]":
        """Kick a serialized launch off as a background task, holding a strong ref so
        asyncio doesn't GC it before it runs. Records the task on the session so
        :meth:`close` can await it (serializing teardown against an in-flight launch).
        Returns the task (tests await it)."""
        task = asyncio.create_task(self._launch(session, restore_tabs=restore_tabs, active_tab=active_tab))
        session._launch_task = task
        self._launch_tasks.add(task)
        task.add_done_callback(self._launch_tasks.discard)
        task.add_done_callback(lambda _t: setattr(session, "_launch_task", None))
        return task

    async def create(self, name: str | None = None) -> LiveBrowser:
        """Start a new browser ('New browser' / fleet ``new``), optionally with a chosen name.

        Registers the browser in ``init`` under ``self._lock`` (cap check FIRST, then name
        resolution + insert -- all atomic) and RETURNS IMMEDIATELY, kicking the serialized
        Chromium launch off as a background task. The route returns ``{name}`` fast; the
        launch flips the browser to ``running`` (and broadcasts) when Chromium is up.

        Cap: ``init`` browsers COUNT toward the cap (a half-started fleet still reserves
        its slots); only crashed shells are excluded (they're dead, kept only to report
        "crashed"). A ``None`` name is generated and regenerated-on-collision against the
        registry (the uniqueness guarantee). A provided name is validated
        (:class:`InvalidBrowserNameError`) and rejected on collision
        (:class:`DuplicateBrowserNameError`).
        """
        async with self._lock:
            # Crashed browsers are dead shells kept only to report "crashed"; they
            # don't count toward the cap, so a crash never blocks opening a new one.
            # init + running both count -- the slot is reserved the moment we register.
            live = sum(1 for browser in self._browsers.values() if not browser._crashed)
            if live >= _MAX_SESSIONS:
                raise FleetFullError(f"{live}/{_MAX_SESSIONS} browsers open -- close one first.")
            if name is None:
                name = self._fresh_name_locked()
            else:
                if not is_valid_browser_name(name):
                    raise InvalidBrowserNameError(
                        f"'{name}' is not a valid browser name -- use lowercase letters, digits, and "
                        "single dashes (e.g. 'alex-smith'), 1-40 characters, no leading/trailing dash."
                    )
                if name in self._browsers:
                    raise DuplicateBrowserNameError(
                        f"the name '{name}' is already in use -- pick another, or close that browser first "
                        "(a crashed browser still holds its name until you close it)."
                    )
            session = self._register_init_locked(name)
        # Persist the manifest NOW, while the browser is still ``init`` (finding [5]):
        # the Chromium launch is multi-second, and a daemon crash in that window would
        # otherwise lose a browser the user just asked for. The init entry has no tabs
        # (it restores to home); the launch's own post-running save then captures its
        # real tabs. Fire-and-forget so create still returns immediately.
        self._spawn_save()
        self._spawn_launch(session)
        return session

    def _fresh_name_locked(self) -> str:
        """A generated name not currently in the live fleet. Caller holds ``self._lock``,
        so the check is against an unchanging ``_browsers`` -- this is the random-name
        uniqueness guarantee. Bounded so a pathological generator can't spin forever
        while holding the global launch lock (which would wedge the whole fleet): after
        a few dozen attempts, fall back to appending a short random suffix."""
        for _ in range(50):
            candidate = generate_browser_name()
            if candidate not in self._browsers:
                return candidate
        # Extremely unlikely (cap is 3); a defensive escape so we never loop unbounded.
        base = generate_browser_name()
        suffix = 1
        while f"{base}-{suffix}" in self._browsers:
            suffix += 1
        return f"{base}-{suffix}"

    def get(self, browser_id: str) -> LiveBrowser:
        # Dict access raises KeyError for a missing/closed name; callers turn it into a 404.
        return self._browsers[browser_id]

    async def resolve(self, browser_id: str) -> LiveBrowser:
        """:meth:`get` as a coroutine, so the sync web layer can resolve a browser ON the
        loop via ``bridge.run`` -- race-free against a concurrent close popping the name --
        without defining its own ``async def``. There is no default browser: every browser
        is created on demand and addressed by name; a closed/unknown name raises KeyError
        (-> 404) and is never reused."""
        return self.get(browser_id)

    def has_browser(self, browser_id: str) -> bool:
        return browser_id in self._browsers

    async def list_browsers(self) -> list[dict[str, Any]]:
        return [await self._browsers[name].describe() for name in sorted(self._browsers)]

    async def close(self, browser_id: str) -> None:
        session = self._browsers.pop(browser_id, None)
        if session is None:
            return
        # Mark closed FIRST, then serialize against an in-flight launch: if create's
        # background _launch is suspended mid-start(), await it so the launch finishes (or
        # aborts via start()'s _abort_start_if_torn_down guard, which now sees _closed)
        # before we tear down -- otherwise a resuming start() could resurrect this removed
        # browser to "running" and leak a second Chromium. The launch holds _startup_lock
        # (not awaited here), so awaiting the task is the right join point.
        session._closed = True
        launch_task = session._launch_task
        if launch_task is not None and launch_task is not asyncio.current_task() and not launch_task.done():
            try:
                await launch_task
            except (asyncio.CancelledError, BrowserStartupError, *_BROWSER_ERRORS) as e:
                logger.debug("in-flight launch of {} unwound during close ({})", browser_id, e)
        await session.close()

    # --- persistence: profiles (Tier A) + manifest (Tier B) -------------------

    def live_browsers(self) -> list[LiveBrowser]:
        """Non-crashed sessions (init + running), by name -- the set that counts toward
        the cap. An ``init`` browser reserves its slot the moment it's registered, so it
        counts here even before Chromium is up.

        Snapshots ``_browsers`` with ``list(...)`` up front so iteration can't
        KeyError if the dict is mutated concurrently (e.g. a close on the loop
        thread popping a name): we sort and filter the snapshot, not the live dict."""
        snapshot = sorted(self._browsers.items())
        return [browser for _, browser in snapshot if not browser._crashed]

    def running_browsers(self) -> list[LiveBrowser]:
        """Only ``running`` sessions, by name -- the set that came up THIS boot with real
        tabs to read. Used by the post-restore reconcile to build fresh entries from
        browsers that actually launched (distinct from the saved-but-not-yet-relaunched
        entries it preserves separately). NOTE: this is no longer the persistence set --
        the durable manifest now snapshots ``live_browsers`` (init + running) so a just-
        created ``init`` browser survives a crash before Chromium is up (finding [5])."""
        snapshot = sorted(self._browsers.items())
        return [browser for _, browser in snapshot if browser._is_running]

    def capacity(self) -> tuple[int, int]:
        """(non-crashed browser count, cap). Counts init + running, mirroring create()'s
        cap check, so the UI gates the 'New browser' button on the same condition
        create() enforces."""
        return len(self.live_browsers()), _MAX_SESSIONS

    async def capacity_async(self) -> tuple[int, int]:
        """``capacity()`` for callers on a Flask worker thread to reach via
        ``bridge.run`` -- running the ``_browsers`` read ON the loop thread (where
        every mutation also happens) is what actually makes it race-free; the
        ``list(...)`` snapshot in ``live_browsers`` is belt-and-suspenders."""
        return self.capacity()

    def _entry_for(self, browser: LiveBrowser) -> fleet_manifest.ManifestEntry:
        """A manifest entry for a live browser: its tab URLs + active tab. Topology
        ONLY -- never ownership/queues (process-scoped) or profile bytes. Uses the
        title-free ``tab_urls()`` so checkpoints don't hammer CDP."""
        urls, active_tab = browser.tab_urls()
        return fleet_manifest.ManifestEntry(id=browser.browser_id, tabs=urls, active_tab=active_tab)

    def _snapshot_manifest_locked(self) -> fleet_manifest.Manifest:
        """Build the durable manifest from the LIVE fleet (init + running). Caller holds
        ``_lock``.

        Init browsers ARE persisted now (finding [5]): a browser the user just created is
        registered ``init`` and its Chromium launch is multi-second, so a daemon crash in
        that window would otherwise lose the browser entirely. Persisting it the moment it
        registers means it is restored next boot (an ``init`` browser has no tabs yet, so
        it restores to the home page -- the same as a fresh create). A persisted entry that
        fails to relaunch is preserved-for-retry by restore's flaked-browser path, not
        stranded; only an explicit ``close`` forgets it. Crashed shells are still excluded
        (they're dead, kept only to report ``crashed`` until the user closes them)."""
        entries = [self._entry_for(browser) for browser in self.live_browsers()]
        return fleet_manifest.Manifest(browsers=entries)

    def _spawn_save(self) -> None:
        """Schedule a manifest checkpoint (fire-and-forget, strong-ref'd). For sync
        callers like the crash hook."""
        async def _do() -> None:
            try:
                await self._save_manifest()
            except (OSError, *_BROWSER_ERRORS) as e:
                logger.debug("crash-triggered manifest checkpoint ignored ({})", e)

        task = asyncio.create_task(_do())
        self._bg_save_tasks.add(task)
        task.add_done_callback(self._bg_save_tasks.discard)

    async def _save_manifest(self) -> None:
        """Checkpoint the manifest if it changed (no-op when nothing did -- idle
        workspaces produce zero backup churn). Snapshots under ``_lock``, writes
        outside it; never called while holding ``_control_lock`` (ownership isn't
        persisted, so there's no lock-ordering hazard)."""
        async with self._lock:
            snapshot = self._snapshot_manifest_locked()
        blob = snapshot.model_dump_json()
        if blob == self._last_manifest_json:
            return
        fleet_manifest.write_manifest(snapshot)
        self._last_manifest_json = blob

    def _scan_profile_names(self) -> list[str]:
        """Browser names that have a persistent profile dir on disk (sorted).

        Only profile suffixes that pass :func:`is_valid_browser_name` are returned.
        That rejects pure-numeric suffixes, so an upgraded workspace's old
        ``browser-use-user-data-dir-0`` / ``-1`` / ``-2`` dirs (from the pre-name
        build) are NOT relaunched as bogus "0"/"1"/"2" named browsers; they fall
        through to the orphan sweep instead."""
        prefix = "browser-use-user-data-dir-"
        names: list[str] = []
        if _PROFILE_ROOT.exists():
            for child in _PROFILE_ROOT.iterdir():
                if not (child.is_dir() and child.name.startswith(prefix)):
                    continue
                suffix = child.name[len(prefix):]
                if is_valid_browser_name(suffix):
                    names.append(suffix)
        return sorted(names)

    def _sweep_orphan_profiles(self, live_names: set[str]) -> None:
        """Delete profile dirs not backing a live browser, to bound Tier-A disk.

        Sweeps both name-valid dirs we no longer want AND legacy numeric dirs from a
        pre-name build (which ``_scan_profile_names`` skips), so an upgrade doesn't
        leave stale numeric profiles around forever."""
        prefix = "browser-use-user-data-dir-"
        if not _PROFILE_ROOT.exists():
            return
        for child in _PROFILE_ROOT.iterdir():
            if not (child.is_dir() and child.name.startswith(prefix)):
                continue
            suffix = child.name[len(prefix):]
            if suffix not in live_names:
                shutil.rmtree(child, ignore_errors=True)

    def forget_profile_dir(self, browser_id: str) -> None:
        """Delete a browser's persistent profile (called on explicit `close`)."""
        shutil.rmtree(_profile_dir(browser_id), ignore_errors=True)

    async def _launch_one_restore(self, name: str, restore_tabs: list[str] | None, active_tab: int) -> bool:
        """Relaunch one saved browser through the SAME register-init -> serialized-launch
        path as ``create``: register it ``init`` under a BRIEF ``_lock`` hold, then await
        its serialized launch (so restore stays eager-sequential -- one Chromium at a
        time). Returns True if it came up ``running``, False if it flaked (the launch
        removed it; left in the manifest for a next-boot retry). Idempotent vs a
        concurrent create that already brought this name up."""
        async with self._lock:
            if name in self._browsers:
                return True  # a concurrent create already brought it up
            live = sum(1 for b in self._browsers.values() if not b._crashed)
            if live >= _MAX_SESSIONS:
                logger.warning("restore hit the fleet cap; deferring browser {}", name)
                return False
            session = self._register_init_locked(name)
        # Await the serialized launch (restore is eager-sequential). persist=False: the
        # post-restore reconcile owns the manifest, so a per-launch save can't race it
        # and drop a flaked-but-wanted browser's preserved entry. On failure ``_launch``
        # removes the browser; we report False so the saved entry is preserved for retry.
        await self._launch(session, restore_tabs=restore_tabs, active_tab=active_tab, persist=False)
        return name in self._browsers and self._browsers[name]._is_running

    async def restore(self) -> None:
        """Bring the fleet back on daemon startup: relaunch saved browsers EAGER-
        SEQUENTIALLY (one at a time -- no cold-boot memory spike; the lock is released
        between launches so read-only routes and ``create`` aren't blocked for the whole
        duration), then reconcile the manifest and sweep TRUE orphan profiles. There is
        NO default browser: a fresh workspace restores to an EMPTY fleet (nothing saved ->
        nothing launched). Read-only routes (ls/state) and ``create`` work during this
        restore -- a create just queues behind the serialized relaunches on ``_lock``.

        Durability rule: a browser that merely flakes on relaunch is NOT forgotten --
        its profile is kept and its manifest entry preserved so it retries next boot.
        Only profiles for names we no longer want are swept.
        """
        saved = fleet_manifest.read_manifest()
        saved_by_name = {e.id: e for e in saved.browsers} if saved is not None else {}
        wanted_names: set[str] = set()

        if saved is not None:
            for entry in sorted(saved.browsers, key=lambda e: e.id):
                wanted_names.add(entry.id)
                await self._launch_one_restore(entry.id, entry.tabs or None, entry.active_tab)
        else:
            # No (current-version) manifest. If name-valid profiles survived on the
            # volume, relaunch them (tabs unknown -> home) rather than wiping the saved
            # logins as a "first boot". Legacy numeric profile dirs are skipped by
            # _scan_profile_names and swept below.
            for profile_name in self._scan_profile_names():
                wanted_names.add(profile_name)
                await self._launch_one_restore(profile_name, None, 0)

        # Reconcile the manifest: fresh snapshots of live browsers + the saved entries
        # for wanted names that FAILED to relaunch (kept so they retry next boot), then
        # sweep only profiles that are neither live nor wanted (true orphans + legacy
        # numeric dirs). A browser created mid-restore is in the live snapshot here (it
        # registered under the same _lock), so it is kept, not dropped.
        await self._reconcile_manifest_after_restore(saved_by_name, wanted_names)

    async def _reconcile_manifest_after_restore(
        self, saved_by_name: dict[str, fleet_manifest.ManifestEntry], wanted_names: set[str]
    ) -> None:
        async with self._lock:
            live_names = {b.browser_id for b in self.running_browsers()}
            entries = [self._entry_for(b) for b in self.running_browsers()]
            # Preserve saved entries for wanted browsers that didn't relaunch this boot.
            for name in sorted(wanted_names - live_names):
                if name in saved_by_name:
                    entries.append(saved_by_name[name])
            entries.sort(key=lambda e: e.id)
            manifest = fleet_manifest.Manifest(browsers=entries)
            # Keep profiles for running + wanted browsers AND any non-crashed browser
            # (e.g. an init created mid-restore whose launch hasn't finished) -- never
            # sweep a profile out from under a browser that's still coming up.
            keep_names = live_names | wanted_names | {b.browser_id for b in self.live_browsers()}
        blob = manifest.model_dump_json()
        if blob != self._last_manifest_json:
            fleet_manifest.write_manifest(manifest)
            self._last_manifest_json = blob
        self._sweep_orphan_profiles(keep_names)

    def start_checkpointing(self) -> None:
        """Begin periodically re-checkpointing the manifest (catches tab-URL drift)."""
        if self._checkpoint_task is None:
            self._checkpoint_task = asyncio.create_task(self._checkpoint_loop())

    async def _checkpoint_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(_MANIFEST_CHECKPOINT_SECONDS)
            try:
                await self._save_manifest()
            except (OSError, *_BROWSER_ERRORS) as e:  # a transient hiccup shouldn't kill the loop
                logger.debug("manifest checkpoint ignored ({})", e)

    async def shutdown(self) -> None:
        self._closed = True
        if self._checkpoint_task is not None:
            self._checkpoint_task.cancel()
        # Final checkpoint so a clean stop captures the latest tabs before teardown.
        try:
            await self._save_manifest()
        except (OSError, *_BROWSER_ERRORS) as e:
            logger.debug("final manifest checkpoint ignored ({})", e)
        for browser_id in list(self._browsers):
            await self.close(browser_id)
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
