import threading
from typing import Any

import httpx
from flask import Flask
from flask import current_app
from loguru import logger
from pydantic import PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.agent_manager import AgentManager
from imbue.system_interface.claude_auth import ClaudeAuthService
from imbue.system_interface.config import Config
from imbue.system_interface.event_queues import AgentEventQueues
from imbue.system_interface.layout_ops import LayoutMutex
from imbue.system_interface.session_watcher import AgentSessionWatcher
from imbue.system_interface.welcome_resend import WelcomeResender
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster

# Key under which the single SystemInterfaceState is stored on ``app.config`` so
# handlers can fetch it via ``get_state()`` without depending on FastAPI-style
# ``app.state`` attribute access.
_STATE_CONFIG_KEY = "SYSTEM_INTERFACE_STATE"


class SystemInterfaceStateError(RuntimeError):
    """Raised when the SystemInterfaceState is not attached to a Flask app."""


class SystemInterfaceState(MutableModel):
    """Holds every shared service handle and config for one system-interface app.

    Replaces FastAPI's ``app.state`` namespace. Built once in
    ``create_application`` and stored on the Flask app; handlers read it via
    ``get_state()``. Owns the per-agent session-watcher registry and the
    latchkey catalog cache (both guarded for concurrent access under the
    threaded WSGI server).
    """

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    config: Config
    provider_names: tuple[str, ...] | None
    include_filters: tuple[str, ...]
    exclude_filters: tuple[str, ...]
    agent_manager: AgentManager
    broadcaster: WebSocketBroadcaster
    event_queues: AgentEventQueues
    layout_mutex: LayoutMutex
    claude_auth_service: ClaudeAuthService
    welcome_resender: WelcomeResender
    http_client: httpx.Client
    latchkey_http_client: httpx.Client
    # Whether this app built (and therefore must start/stop) the agent manager.
    # False when a preconfigured manager was injected (tests).
    is_agent_manager_owned: bool
    watchers: dict[str, AgentSessionWatcher] = {}
    latchkey_catalog_cache: dict[str, Any] = {}

    _watchers_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _latchkey_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _is_shut_down: bool = PrivateAttr(default=False)

    @property
    def latchkey_lock(self) -> threading.Lock:
        """Serializes concurrent latchkey catalog fetches across request threads."""
        return self._latchkey_lock

    def get_or_create_watcher(self, agent_info: AgentInfo) -> AgentSessionWatcher:
        """Get the existing session watcher for an agent, or create and start one.

        Guarded by a lock so two concurrent request threads cannot both build a
        watcher for the same agent under the threaded server.
        """
        with self._watchers_lock:
            existing = self.watchers.get(agent_info.id)
            if existing is not None:
                return existing

            # Single-element holder so the ``on_events`` closure can reach the
            # watcher we are about to construct. Capturing the watcher directly
            # (rather than looking it up by id on every event) keeps the
            # callback self-contained: it cannot KeyError if the dict entry has
            # since been removed, and does not depend on the entry already
            # existing before the first event fires.
            watcher_holder: list[AgentSessionWatcher] = []

            def on_events(agent_id: str, events: list[dict[str, Any]]) -> None:
                # IGNORE: session events are persisted in JSONL and recoverable
                # via the REST /events endpoint; storing them in the in-memory
                # replay buffer would grow unboundedly for the agent's lifetime.
                self.event_queues.broadcast_all_ignored(agent_id, events)
                # Recompute per-agent activity state from the full transcript.
                # The watcher's incremental ``events`` argument only contains the
                # newest lines, but the activity tracker needs the full
                # transcript to detect unmatched tool_uses across turns and to
                # read the last event's type.
                self.agent_manager.update_session_events(agent_id, watcher_holder[0].get_all_events())

            watcher = AgentSessionWatcher(
                agent_id=agent_info.id,
                agent_state_dir=agent_info.agent_state_dir,
                claude_config_dir=agent_info.claude_config_dir,
                on_events=on_events,
            )
            watcher_holder.append(watcher)
            self.watchers[agent_info.id] = watcher
            watcher.start()

        # Seed transcript-derived activity signals once at watcher creation so
        # the indicator does not lag a turn behind on first connect. Done
        # outside the watchers lock to avoid holding it across the agent
        # manager's own lock.
        self.agent_manager.update_session_events(agent_info.id, watcher.get_all_events())
        return watcher

    def stop_all_watchers(self) -> None:
        with self._watchers_lock:
            for watcher in self.watchers.values():
                watcher.stop()
            self.watchers.clear()

    def shutdown(self) -> None:
        """Tear down every owned resource. Idempotent."""
        if self._is_shut_down:
            return
        self._is_shut_down = True
        self.event_queues.shutdown()
        self.broadcaster.shutdown()
        if self.is_agent_manager_owned:
            self.agent_manager.stop()
        self.stop_all_watchers()
        try:
            self.http_client.close()
        except (httpx.HTTPError, RuntimeError) as e:
            logger.debug("Skipped closing service http client during shutdown: {}", e)
        try:
            self.latchkey_http_client.close()
        except (httpx.HTTPError, RuntimeError) as e:
            logger.debug("Skipped closing latchkey http client during shutdown: {}", e)


def attach_state(app: Flask, state: SystemInterfaceState) -> None:
    app.config[_STATE_CONFIG_KEY] = state


def get_state() -> SystemInterfaceState:
    """Return the SystemInterfaceState for the current Flask app."""
    state = current_app.config.get(_STATE_CONFIG_KEY)
    if not isinstance(state, SystemInterfaceState):
        raise SystemInterfaceStateError("SystemInterfaceState is not attached to the current app")
    return state


def state_of(app: Flask) -> SystemInterfaceState:
    """Return the SystemInterfaceState attached to ``app`` without needing an app context."""
    state = app.config.get(_STATE_CONFIG_KEY)
    if not isinstance(state, SystemInterfaceState):
        raise SystemInterfaceStateError("SystemInterfaceState is not attached to the app")
    return state
