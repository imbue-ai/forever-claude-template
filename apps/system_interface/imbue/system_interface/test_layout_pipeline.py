"""Acceptance test for the agent-driven layout pipeline.

Exercises the full backend path the agent-facing helper depends on:
``scripts/layout.py`` (subprocess) -> ``POST /api/layout/broadcast``
(loopback Flask route) -> ``WebSocketBroadcaster.broadcast_layout_op``.
The WS-to-DOM step is left to manual verification (no headless browser
harness exists for the dockview layout).

Covers ``inspect`` (pure read; bypasses the mutex and broadcaster) and
``open``/``close`` (mutating ops that acquire the advisory mutex and
broadcast). Broadcaster output is observed via the broadcaster's own
queue-registration API rather than a live WebSocket -- the assertion
target is "the broadcaster received a well-formed message", not "a
WebSocket transport delivered it", and the latter is already exercised
by the broadcaster's own tests.
"""

from __future__ import annotations

import json
import queue as queue_module
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from typing import Generator

import pytest

from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.agent_manager import AgentManager
from imbue.system_interface.config import Config
from imbue.system_interface.models import AgentStateItem
from imbue.system_interface.server import create_application
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster
from imbue.system_interface.wsgi import make_threaded_server

pytestmark = pytest.mark.acceptance

_PORT = 18766
_BASE_URL = f"http://127.0.0.1:{_PORT}"
_AGENT_ID = "test-agent-id"
_AGENT_NAME = "alice"

_REPO_ROOT = Path(__file__).resolve().parents[4]
_LAYOUT_SCRIPT = _REPO_ROOT / "scripts" / "layout.py"


def _server_is_up(url: str) -> bool:
    """Probe ``/api/layout`` and treat a 200 or 404 as "lifespan finished".

    Hits ``/api/layout`` rather than ``/api/agents`` because the latter
    calls ``discover_agents``, which reads the real mngr config and can
    fail with HTTP 500 in dev environments. ``/api/layout`` is a pure
    file-system read against the redirected ``MNGR_HOST_DIR``. A 404
    here means the lifespan has run (so ``app.state.broadcaster`` /
    ``app.state.layout_mutex`` are set), which is what later assertions
    actually depend on.
    """
    try:
        urllib.request.urlopen(f"{url}/api/layout", timeout=0.5)
        return True
    except urllib.error.HTTPError as e:
        return e.code == 404
    except OSError:
        return False


@pytest.fixture
def layout_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[tuple[str, WebSocketBroadcaster], None, None]:
    """Spin up a workspace_server with a preconfigured AgentManager.

    The manager is seeded with a single known agent so that ``list`` and
    refs like ``chat:alice`` resolve. ``MNGR_HOST_DIR`` is redirected to
    a tmp dir so the test does not touch the real host state.
    """
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path / "host"))
    monkeypatch.setenv("MNGR_AGENT_ID", _AGENT_ID)
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(tmp_path / "work"))

    broadcaster = WebSocketBroadcaster()
    manager = AgentManager.build(broadcaster)
    manager._agents[_AGENT_ID] = AgentStateItem(
        id=_AGENT_ID,
        name=_AGENT_NAME,
        state="running",
        labels={},
        work_dir=str(tmp_path / "work"),
    )

    config = Config(system_interface_host="127.0.0.1", system_interface_port=_PORT)
    app = create_application(config=config, agent_manager=manager)

    server = make_threaded_server("127.0.0.1", _PORT, app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        wait_for(
            lambda: _server_is_up(_BASE_URL),
            timeout=5.0,
            poll_interval=0.05,
            error_message=f"workspace server did not come up at {_BASE_URL}",
        )
        yield _BASE_URL, broadcaster
    finally:
        server.shutdown()
        thread.join(timeout=5.0)


def _run_layout_script(
    args: list[str],
    base_url: str,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    """Invoke ``scripts/layout.py`` as a subprocess against the test server.

    ``cwd`` is set to a sandbox tmp path so the script's relative
    ``runtime/applications.toml`` lookup does not pick up the real one
    from the repo. ``MINDS_WORKSPACE_SERVER_URL`` points at the test
    server.
    """
    return subprocess.run(
        [sys.executable, str(_LAYOUT_SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env={
            "PATH": sys.exec_prefix + "/bin:/usr/bin:/bin",
            "PYTHONPATH": "",
            "MINDS_WORKSPACE_SERVER_URL": base_url,
            "MNGR_AGENT_ID": _AGENT_ID,
            # Mutating ops in production block until the layout state
            # changes are observable via inspect; this test exercises
            # the broadcast pipeline without a live frontend to apply
            # the op, so we tell the script not to wait. Documented in
            # ``scripts/layout.py`` under ``ENV_NO_WAIT_STABLE``.
            "MINDS_LAYOUT_NO_WAIT_STABLE": "1",
        },
        timeout=15,
    )


def _await_layout_op(client_queue: queue_module.Queue[str | None], timeout: float) -> dict[str, Any]:
    """Block until a ``layout_op`` message arrives, returning the parsed payload.

    Non-``layout_op`` messages (e.g. ``agents_updated``) that race with the
    test's setup are skipped silently.
    """
    deadline_message = f"no layout_op message arrived within {timeout}s"
    parsed_result: dict[str, Any] = {}

    def _drain_once() -> bool:
        try:
            msg = client_queue.get(timeout=0.05)
        except queue_module.Empty:
            return False
        assert msg is not None, "broadcaster shut down before a layout_op arrived"
        parsed = json.loads(msg)
        if parsed.get("type") != "layout_op":
            return False
        parsed_result.update(parsed)
        return True

    wait_for(_drain_once, timeout=timeout, poll_interval=0.0, error_message=deadline_message)
    return parsed_result


def test_inspect_round_trips_through_script_and_endpoint(
    layout_server: tuple[str, WebSocketBroadcaster],
    tmp_path: Path,
) -> None:
    """``scripts/layout.py inspect --json`` returns parseable JSON for an empty layout."""
    base_url, _ = layout_server
    sandbox = tmp_path / "cwd"
    sandbox.mkdir()

    result = _run_layout_script(["inspect", "--json"], base_url=base_url, cwd=sandbox)

    assert result.returncode == 0, f"stderr={result.stderr!r}"
    parsed = json.loads(result.stdout)
    assert parsed == {"active_panel": None, "panels": [], "tree": None}


def test_list_round_trips_and_includes_seeded_agent(
    layout_server: tuple[str, WebSocketBroadcaster],
    tmp_path: Path,
) -> None:
    """``scripts/layout.py list --json`` returns the seeded agent as a ``chat:`` entry."""
    base_url, _ = layout_server
    sandbox = tmp_path / "cwd"
    sandbox.mkdir()

    result = _run_layout_script(["list", "--json"], base_url=base_url, cwd=sandbox)

    assert result.returncode == 0, f"stderr={result.stderr!r}"
    entries = json.loads(result.stdout)
    refs = {e["ref"] for e in entries}
    assert f"chat:{_AGENT_NAME}" in refs


def test_open_terminal_returns_ref_via_stdout_and_broadcasts_panel_id(
    layout_server: tuple[str, WebSocketBroadcaster],
    tmp_path: Path,
) -> None:
    """Full pipeline check for the synchronous-ref-return path.

    ``scripts/layout.py open terminal`` must (a) print the
    ``terminal:<hash>`` ref the server allocated to stdout so the
    calling agent can capture it, and (b) cause the broadcast to carry
    the matching ``panel_id`` so the frontend uses the same id the ref
    was derived from.
    """
    base_url, broadcaster = layout_server
    sandbox = tmp_path / "cwd"
    sandbox.mkdir()
    # The script polls runtime/applications.toml for the named service;
    # seed ``terminal`` so registration succeeds without the real
    # forward_port pipeline.
    applications_dir = sandbox / "runtime"
    applications_dir.mkdir()
    (applications_dir / "applications.toml").write_text(
        '[[applications]]\nname = "terminal"\nurl = "http://localhost:9000/terminal"\n'
    )

    client_queue = broadcaster.register()
    try:
        result = _run_layout_script(["open", "terminal"], base_url=base_url, cwd=sandbox)
        assert result.returncode == 0, f"stderr={result.stderr!r}"
        printed_ref = result.stdout.strip()
        assert printed_ref.startswith("terminal:"), printed_ref

        msg = _await_layout_op(client_queue, timeout=2.0)
        assert msg["op"] == "open"
        broadcast_args = msg["args"]
        assert broadcast_args["ref"] == "service:terminal"
        # The same panel id was used to derive the printed ref AND sent
        # to the frontend, so the resulting tab is the one the script
        # told the caller about.
        assert broadcast_args["panel_id"].startswith("iframe-terminal-")
    finally:
        broadcaster.unregister(client_queue)


def test_open_close_chat_ref_broadcasts_layout_ops(
    layout_server: tuple[str, WebSocketBroadcaster],
    tmp_path: Path,
) -> None:
    """``open`` and ``close`` against a ``chat:`` ref reach the broadcaster intact."""
    base_url, broadcaster = layout_server
    sandbox = tmp_path / "cwd"
    sandbox.mkdir()

    client_queue = broadcaster.register()
    try:
        open_result = _run_layout_script(["open", f"chat:{_AGENT_NAME}"], base_url=base_url, cwd=sandbox)
        assert open_result.returncode == 0, f"stderr={open_result.stderr!r}"
        open_msg = _await_layout_op(client_queue, timeout=2.0)
        assert open_msg["op"] == "open"
        # ``_cmd_open`` always sends ``new_group``; the broadcaster passes
        # the args dict through unchanged.
        assert open_msg["args"] == {"ref": f"chat:{_AGENT_NAME}", "new_group": False}

        close_result = _run_layout_script(["close", f"chat:{_AGENT_NAME}"], base_url=base_url, cwd=sandbox)
        assert close_result.returncode == 0, f"stderr={close_result.stderr!r}"
        close_msg = _await_layout_op(client_queue, timeout=2.0)
        assert close_msg["op"] == "close"
        assert close_msg["args"] == {"ref": f"chat:{_AGENT_NAME}"}
    finally:
        broadcaster.unregister(client_queue)


def test_open_chat_terminal_ref_broadcasts_through_pipeline(
    layout_server: tuple[str, WebSocketBroadcaster],
    tmp_path: Path,
) -> None:
    """``open chat-terminal:<name>`` reaches the broadcaster with the ref intact.

    Covers the new agent-attached terminal ref end-to-end: the script's
    ref-prefix table must include ``chat-terminal:``, the broadcast
    endpoint must accept it (no service registration check, no
    ``service:terminal`` panel_id allocation), and the args must arrive
    unchanged so the frontend can resolve to the per-agent terminal URL.
    """
    base_url, broadcaster = layout_server
    sandbox = tmp_path / "cwd"
    sandbox.mkdir()

    client_queue = broadcaster.register()
    try:
        result = _run_layout_script(["open", f"chat-terminal:{_AGENT_NAME}"], base_url=base_url, cwd=sandbox)
        assert result.returncode == 0, f"stderr={result.stderr!r}"
        msg = _await_layout_op(client_queue, timeout=2.0)
        assert msg["op"] == "open"
        assert msg["args"] == {"ref": f"chat-terminal:{_AGENT_NAME}", "new_group": False}
    finally:
        broadcaster.unregister(client_queue)


def test_list_includes_chat_terminal_entry_for_seeded_agent(
    layout_server: tuple[str, WebSocketBroadcaster],
    tmp_path: Path,
) -> None:
    """``list`` surfaces the per-agent terminal alongside the chat entry.

    Discoverability: an agent listing without the terminal would force
    callers to know about the ``chat-terminal:`` form out of band.
    """
    base_url, _ = layout_server
    sandbox = tmp_path / "cwd"
    sandbox.mkdir()

    result = _run_layout_script(["list", "--json"], base_url=base_url, cwd=sandbox)
    assert result.returncode == 0, f"stderr={result.stderr!r}"
    entries = json.loads(result.stdout)
    by_ref = {e["ref"]: e for e in entries}
    assert f"chat-terminal:{_AGENT_NAME}" in by_ref
    assert by_ref[f"chat-terminal:{_AGENT_NAME}"]["kind"] == "agent-terminal"
