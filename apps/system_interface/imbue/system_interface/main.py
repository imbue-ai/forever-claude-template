import argparse
import atexit
import signal
from collections.abc import Sequence
from types import FrameType

import httpx
from flask import Flask

from imbue.system_interface.agent_manager import AgentManager
from imbue.system_interface.app_context import SystemInterfaceState
from imbue.system_interface.app_context import get_state
from imbue.system_interface.claude_auth import ClaudeAuthService
from imbue.system_interface.config import Config
from imbue.system_interface.config import load_config
from imbue.system_interface.event_queues import AgentEventQueues
from imbue.system_interface.layout_ops import LayoutMutex
from imbue.system_interface.server import create_application
from imbue.system_interface.welcome_resend import WelcomeResender
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster
from imbue.system_interface.wsgi import make_threaded_server


def _exit_on_signal(signum: int, frame: FrameType | None) -> None:
    """Turn SIGTERM/SIGINT into a clean exit so the ``atexit`` teardown runs.

    The shutdown itself (broadcaster, watchers, agent manager, http clients) is
    registered via ``atexit`` in ``main``; raising ``SystemExit`` here ensures
    that interpreter-exit path runs instead of the default abrupt termination.
    """
    raise SystemExit(0)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="System Interface")
    parser.add_argument("--provider", action="append", default=[], help="Filter agents by provider name (repeatable)")
    parser.add_argument("--include", action="append", default=[], help="CEL include filter for agents (repeatable)")
    parser.add_argument("--exclude", action="append", default=[], help="CEL exclude filter for agents (repeatable)")
    return parser.parse_args(argv)


def build_production_state(
    config: Config,
    provider_names: tuple[str, ...] | None = None,
    include_filters: tuple[str, ...] = (),
    exclude_filters: tuple[str, ...] = (),
) -> SystemInterfaceState:
    """Construct the real object graph -- the composition root.

    This is the single place the production collaborators are wired together.
    It builds but does not start the agent manager (``main`` starts it once the
    app is assembled), so it spawns no ``mngr observe`` pipeline by itself.
    Tests do not use this; they build a ``SystemInterfaceState`` with fakes via
    ``testing.build_test_state``.
    """
    broadcaster = WebSocketBroadcaster()
    agent_manager = AgentManager.build(broadcaster)
    welcome_resender = WelcomeResender(
        resolve_agent=agent_manager.get_agent_info_by_id,
        send_message_fn=agent_manager.send_message_to_agent,
    )
    return SystemInterfaceState(
        config=config,
        provider_names=provider_names,
        include_filters=include_filters,
        exclude_filters=exclude_filters,
        agent_manager=agent_manager,
        event_queues=AgentEventQueues(),
        # Advisory in-process mutex serializing layout-mutating ops. The agent
        # script never auto-retries on contention -- it surfaces the 409 to the
        # agent along with the in-flight holder's metadata.
        layout_mutex=LayoutMutex(),
        # One long-lived ClaudeAuthService per app so the in-flight OAuth
        # subprocess survives between the /start and /submit-code requests.
        # The service consults the resender before an auth-apply restart so a
        # never-welcomed chat agent restarts idle (the welcome resend is its
        # resumption) instead of receiving the "please continue" message.
        claude_auth_service=ClaudeAuthService(
            resolve_never_welcomed_agent_name=welcome_resender.never_welcomed_agent_name,
        ),
        welcome_resender=welcome_resender,
        # Single shared synchronous httpx client for the /service/<name>/
        # forwarding layer; a separate one for the latchkey catalog proxy.
        http_client=httpx.Client(follow_redirects=False, timeout=30.0),
        latchkey_http_client=httpx.Client(timeout=30.0),
    )


def build_application(config: Config, args: argparse.Namespace) -> Flask:
    """Build the Flask app from parsed CLI args, threading the agent filters through.

    Wires the production object graph and assembles the app, but does not start
    the agent manager's ``mngr observe`` pipeline -- ``main`` does that once the
    app is built.
    """
    state = build_production_state(
        config,
        provider_names=tuple(args.provider) if args.provider else None,
        include_filters=tuple(args.include),
        exclude_filters=tuple(args.exclude),
    )
    return create_application(state)


def main() -> None:
    """Run the system-interface server."""
    args = _parse_args(None)

    config = load_config()
    application = build_application(config, args)
    with application.app_context():
        state = get_state()

    # Start the ``mngr observe`` pipeline now that the app is assembled. This is
    # the one place observe is started; ``build_application`` only constructs, so
    # tests that build an app never spawn it.
    state.agent_manager.start()

    # Tear down the broadcaster, watchers, agent manager, and http clients on
    # exit. ``atexit`` covers a normal return; the signal handlers cover
    # supervisord's SIGTERM and an interactive SIGINT (Ctrl-C), which
    # ``run_simple`` would otherwise turn into an abrupt exit.
    atexit.register(state.shutdown)
    signal.signal(signal.SIGTERM, _exit_on_signal)
    signal.signal(signal.SIGINT, _exit_on_signal)

    # Threaded HTTP/1.1 server: each request (and each long-lived SSE/WebSocket
    # connection) owns its own OS thread, which is what flask-sock needs and what
    # replaces uvicorn's single asyncio event loop. HTTP/1.1 (vs werkzeug's
    # HTTP/1.0 default) is required for keepalive and incremental SSE streaming.
    server = make_threaded_server(
        config.system_interface_host,
        config.system_interface_port,
        application,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
