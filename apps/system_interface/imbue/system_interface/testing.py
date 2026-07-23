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

import re
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
from imbue.system_interface.agent_discovery import get_host_dir
from imbue.system_interface.agent_manager import AgentManager
from imbue.system_interface.app_context import SystemInterfaceState
from imbue.system_interface.chat_file_timestamps import ChatFileTimestampStore
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
        # Under the autouse test isolation fixture MNGR_HOST_DIR points at a
        # fresh tmp dir, so each test gets its own empty fingerprint store.
        chat_file_timestamps=ChatFileTimestampStore(get_host_dir() / "chat_file_timestamps"),
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
    """Records the inputs the OAuth flow sends to a `pexpect.spawn`.

    Constructor arguments parameterize how the fake responds to `expect()`:

    - `url_match`: when non-None, the first `expect()` returns
      `expect_return_index` (default 0 for the URL-matched branch) and
      `self.match` is preset to the result of regex-matching `url_match`.
      When None, the first `expect()` returns `expect_return_index`
      (typically 1 for EOF or 2 for TIMEOUT) without setting `match`.
    - `raw_output`: the bytes the real CLI left in the consumed buffer
      (`process.before + process.after`). Defaults to `url_match` (a bare
      URL), but a test can inject the escape-wrapped OSC 8 hyperlink the
      real `claude auth login` emits to exercise the URL-extraction path.
    - `expect_return_index`: index returned on the first `expect()` call.
      Lets a test simulate the URL-found / EOF-before-URL / timeout
      branches of `_spawn_oauth_and_parse_url`.
    - `eof_return_index`: index returned on every subsequent `expect()`
      call. Defaults to 0 (the EOF branch in `_drive_oauth_code`'s
      `[pexpect.EOF, pexpect.TIMEOUT]` pattern) so the post-code-submit
      teardown lands in the success path.
    """

    def __init__(
        self,
        url_match: str | None = None,
        expect_return_index: int = 0,
        eof_return_index: int = 0,
        raw_output: str | None = None,
    ) -> None:
        self._expect_return_index = expect_return_index
        self._eof_return_index = eof_return_index
        self._expect_call_count = 0
        self.sendline_calls: list[str] = []
        self.terminate_calls = 0
        self.close_calls = 0
        self.timeout: float | None = None
        self.match: re.Match[str] | None = None
        # Mirror what pexpect leaves after a successful match: everything it
        # consumed lives in `before` + `after`. `_spawn_oauth_and_parse_url`
        # reads that pair, so the fake drives extraction through `after`.
        self.before = ""
        self.after = raw_output if raw_output is not None else (url_match or "")
        if url_match is not None:
            self.match = re.compile(r".*").match(url_match)
            assert self.match is not None

    def expect(self, _patterns: object) -> int:
        self._expect_call_count += 1
        if self._expect_call_count == 1:
            return self._expect_return_index
        return self._eof_return_index

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
