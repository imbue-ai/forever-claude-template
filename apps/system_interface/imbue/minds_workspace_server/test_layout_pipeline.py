"""Acceptance test for the agent-driven layout pipeline.

Exercises the full backend path the agent-facing helper depends on:
``scripts/layout.py`` (subprocess) -> ``POST /api/layout/broadcast``
(loopback FastAPI route) -> ``WebSocketBroadcaster.broadcast_layout_op``.
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
import uvicorn

from imbue.minds_workspace_server.agent_manager import AgentManager
from imbue.minds_workspace_server.config import Config
from imbue.minds_workspace_server.models import AgentStateItem
from imbue.minds_workspace_server.server import create_application
from imbue.minds_workspace_server.ws_broadcaster import WebSocketBroadcaster
from imbue.mngr.utils.polling import wait_for

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

    config = Config(minds_workspace_server_host="127.0.0.1", minds_workspace_server_port=_PORT)
    app = create_application(config=config, agent_manager=manager)

    server = uvicorn.Server(uvicorn.Config(app=app, host="127.0.0.1", port=_PORT, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
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
        server.should_exit = True
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
    assert parsed == {"panels": [], "tree": None}


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
        open_result = _run_layout_script(
            ["open", f"chat:{_AGENT_NAME}"], base_url=base_url, cwd=sandbox
        )
        assert open_result.returncode == 0, f"stderr={open_result.stderr!r}"
        open_msg = _await_layout_op(client_queue, timeout=2.0)
        assert open_msg["op"] == "open"
        # ``_cmd_open`` always sends ``new_group``; the broadcaster passes
        # the args dict through unchanged.
        assert open_msg["args"] == {"ref": f"chat:{_AGENT_NAME}", "new_group": False}

        close_result = _run_layout_script(
            ["close", f"chat:{_AGENT_NAME}"], base_url=base_url, cwd=sandbox
        )
        assert close_result.returncode == 0, f"stderr={close_result.stderr!r}"
        close_msg = _await_layout_op(client_queue, timeout=2.0)
        assert close_msg["op"] == "close"
        assert close_msg["args"] == {"ref": f"chat:{_AGENT_NAME}"}
    finally:
        broadcaster.unregister(client_queue)
