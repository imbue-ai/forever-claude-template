from collections.abc import Callable
from typing import Any

import pluggy
from flask import Flask

hookspec = pluggy.HookspecMarker("system_interface")
hookimpl = pluggy.HookimplMarker("system_interface")

EventBroadcaster = Callable[[str, dict[str, Any]], None]


class SystemInterfaceHookSpec:
    @hookspec
    def endpoint(self, app: Flask) -> None:
        """Register additional endpoints on the Flask application."""

    @hookspec
    def register_event_broadcaster(self, broadcaster: EventBroadcaster) -> None:
        """Receive a reference to the event broadcaster for injecting events."""
