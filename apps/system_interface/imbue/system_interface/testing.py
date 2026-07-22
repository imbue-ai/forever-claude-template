"""Shared test fakes for the system_interface package.

Houses deterministic stand-ins for outside-world dependencies that
`ClaudeAuthService` takes as constructor-injected callables
(`command_runner`, `pexpect_spawner`). Both `claude_auth_test.py` and
`claude_auth_endpoints_test.py` need the same fakes, so they live here
rather than being copy-pasted into each test module.

Also houses `build_test_state`, the test-side composition root: it builds a
`SystemInterfaceState` with fakes for whichever collaborators a test overrides
and cheap real instances for the rest, mirroring `main.build_production_state`
without ever starting the agent manager.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from collections.abc import Sequence
from contextlib import closing
from contextlib import contextmanager

import httpx
import simple_websocket
from flask import Flask

from imbue.mngr.api.find import AgentMatch
from imbue.mngr.primitives import AgentId
from imbue.system_interface.agent_discovery import MngrMessenger
from imbue.system_interface.agent_manager import AgentManager
from imbue.system_interface.app_context import SystemInterfaceState
from imbue.system_interface.claude_auth import ClaudeAuthService
from imbue.system_interface.config import Config
from imbue.system_interface.event_queues import AgentEventQueues
from imbue.system_interface.layout_ops import LayoutMutex
from imbue.system_interface.welcome_resend import WelcomeResender
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster
from imbue.system_interface.wsgi import make_threaded_server


class RecordingMngrMessenger(MngrMessenger):
    """A `MngrMessenger` that records sends and never contacts mngr.

    Overrides `send_to_agent` to record each `(agent_id, message)` and return a
    fixed result, so a test exercises the manager's send path without building a
    real mngr context or hitting the network. Inject via
    `AgentManager.build(broadcaster, messenger=RecordingMngrMessenger())`.
    """

    sent: list[tuple[str, str]] = []
    succeeds: bool = True

    def send_to_agent(self, agent_id: AgentId, message: str, known_locations: Sequence[AgentMatch]) -> bool:
        self.sent.append((str(agent_id), message))
        return self.succeeds


def build_test_state(
    *,
    config: Config | None = None,
    agent_manager: AgentManager | None = None,
    claude_auth_service: ClaudeAuthService | None = None,
    welcome_resender: WelcomeResender | None = None,
    latchkey_http_client: httpx.Client | None = None,
) -> SystemInterfaceState:
    """Build a `SystemInterfaceState` for tests, injecting fakes where provided.

    Every collaborator left unset gets a cheap default production instance;
    pass one to substitute a fake. The agent manager is built but never started,
    so no `mngr observe` pipeline is spawned. The state's broadcaster is derived
    from the agent manager, so injecting `agent_manager` (often built with a fake
    `MngrMessenger`) repoints the broadcaster too.

    Only the collaborators tests actually override are parameters; the agent
    filters and the service-proxy http client (which no test substitutes) are
    fixed to their production defaults inline.
    """
    manager = agent_manager if agent_manager is not None else AgentManager.build(WebSocketBroadcaster())
    return SystemInterfaceState(
        config=config if config is not None else Config(),
        provider_names=None,
        include_filters=(),
        exclude_filters=(),
        agent_manager=manager,
        event_queues=AgentEventQueues(),
        layout_mutex=LayoutMutex(),
        claude_auth_service=claude_auth_service if claude_auth_service is not None else ClaudeAuthService(),
        welcome_resender=welcome_resender
        if welcome_resender is not None
        else WelcomeResender(
            resolve_agent=manager.get_agent_info_by_id,
            send_message_fn=manager.send_message_to_agent,
        ),
        http_client=httpx.Client(follow_redirects=False, timeout=30.0),
        latchkey_http_client=latchkey_http_client if latchkey_http_client is not None else httpx.Client(timeout=30.0),
    )


class FakeFinishedProcess:
    """Minimal stand-in for a `FinishedProcess` returned by `command_runner`.

    The real subprocess runner produces an object with `stdout`, `stderr`,
    and `returncode`; this class exposes just those three so tests can
    drive every branch the `claude_auth` callers care about.
    """

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class FakePexpectProcess:
    """Scripted stand-in for a `pexpect.spawn` in the setup-token flow.

    `expect_script` is a sequence of `(return_index, output_chunk)` pairs:
    each `expect()` call consumes the next entry (the final entry repeats
    once the script is exhausted), returns `return_index`, and exposes
    `output_chunk` through `before`/`after` the way pexpect does after a
    match (index 0: chunk in `after`) or a non-match (chunk in `before`).
    Callers can then script the URL-print, poll-pending (TIMEOUT), and
    token-print stages of `claude setup-token` deterministically.
    """

    def __init__(self, expect_script: Sequence[tuple[int, str]]) -> None:
        assert expect_script, "expect_script must have at least one entry"
        self._script = list(expect_script)
        self._call_idx = 0
        self.sendline_calls: list[str] = []
        self.terminate_calls = 0
        self.close_calls = 0
        self.timeout: float | None = None
        self.before = ""
        self.after: str = ""

    def expect(self, _patterns: object, timeout: float | None = None) -> int:
        entry_idx = min(self._call_idx, len(self._script) - 1)
        self._call_idx += 1
        return_index, chunk = self._script[entry_idx]
        if return_index == 0:
            self.before = ""
            self.after = chunk
        else:
            self.before = chunk
            self.after = ""
        return return_index

    def sendline(self, s: str) -> None:
        self.sendline_calls.append(s)

    def isalive(self) -> bool:
        return True

    def terminate(self, force: bool = False) -> None:
        self.terminate_calls += 1

    def close(self) -> None:
        self.close_calls += 1


def _find_free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait_until_serving(host: str, port: int, timeout: float = 10.0) -> None:
    """Poll a TCP connect until the server accepts, or raise on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with closing(socket.create_connection((host, port), timeout=0.5)):
                return
        except OSError:
            time.sleep(0.02)
    raise TimeoutError(f"server at {host}:{port} did not start within {timeout}s")


class ServedApp:
    """Handle to a Flask app served by a real Werkzeug listener in a background thread."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port

    @property
    def http_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}"


@contextmanager
def serve_app(app: Flask) -> Iterator[ServedApp]:
    """Serve ``app`` on an ephemeral loopback port via a real threaded Werkzeug server.

    Used by the WebSocket/SSE tests, which the Flask test client cannot drive
    (flask-sock needs a real listener). The server runs in a daemon thread and
    is shut down on exit.
    """
    host = "127.0.0.1"
    port = _find_free_port()
    server = make_threaded_server(host, port, app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _wait_until_serving(host, port)
        yield ServedApp(host, port)
    finally:
        server.shutdown()
        thread.join(timeout=5.0)


def open_ws(served: ServedApp, path: str, subprotocols: list[str] | None = None) -> simple_websocket.Client:
    """Open a WebSocket client against a ``ServedApp`` at ``path``."""
    return simple_websocket.Client(f"{served.ws_url}{path}", subprotocols=subprotocols)


def close_ws(ws: simple_websocket.Client) -> None:
    """Close a WebSocket client, tolerating an already-closed connection.

    A handler that finishes (e.g. the proto-agent-logs not-found path) closes
    the socket server-side first, so the client-side close would otherwise raise
    ``ConnectionClosed``.
    """
    try:
        ws.close()
    except simple_websocket.ConnectionClosed:
        pass
