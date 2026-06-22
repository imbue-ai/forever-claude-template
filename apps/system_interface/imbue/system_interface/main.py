import argparse
import atexit
import signal
from collections.abc import Sequence
from types import FrameType

from flask import Flask

from imbue.system_interface.app_context import get_state
from imbue.system_interface.config import Config
from imbue.system_interface.config import load_config
from imbue.system_interface.server import create_application
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


def build_application(config: Config, args: argparse.Namespace) -> Flask:
    """Build the Flask app from parsed CLI args, threading the agent filters through."""
    return create_application(
        config,
        provider_names=tuple(args.provider) if args.provider else None,
        include_filters=tuple(args.include),
        exclude_filters=tuple(args.exclude),
    )


def main() -> None:
    """Run the system-interface server."""
    args = _parse_args(None)

    config = load_config()
    application = build_application(config, args)

    # Tear down the broadcaster, watchers, agent manager, and http clients on
    # exit. ``atexit`` covers a normal return; the signal handlers cover
    # supervisord's SIGTERM and an interactive SIGINT (Ctrl-C), which
    # ``run_simple`` would otherwise turn into an abrupt exit.
    with application.app_context():
        state = get_state()
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
