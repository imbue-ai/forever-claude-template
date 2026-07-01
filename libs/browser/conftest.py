import pytest
from browser import manifest as _manifest
from browser import runner as _runner
from browser import session as _session


@pytest.fixture(scope="session", autouse=True)
def _bridge_running():
    """Start the single background asyncio loop once for the whole test session.

    The Flask HTTP-layer tests reach session.py coroutines through ``runner.bridge``
    (``bridge.run`` / ``bridge.submit``), so the loop must be live. Session-level
    unit tests that use ``asyncio.run`` directly don't need it, but starting it once
    is cheap and harmless. Stopped at teardown.
    """
    _runner.bridge.start()
    yield
    _runner.bridge.stop()


@pytest.fixture(autouse=True)
def _isolate_browser_persistence(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Keep tests off the real workspace volume and ``runtime/``.

    Redirects each browser's persistent Chromium profile root and the fleet manifest
    into a per-test tmp dir, and opens the daemon's init gate by default -- the real
    startup restore doesn't run under a bare Flask test client, so without this every
    state-changing route would 503. A test that wants to exercise the gate clears
    ``runner._init_done`` itself.
    """
    monkeypatch.setattr(_session, "_PROFILE_ROOT", tmp_path / "profiles")
    monkeypatch.setattr(_manifest, "_MANIFEST_PATH", tmp_path / "browser-fleet.json")
    # Start each test with a clean shared daemon manager so a fake browser installed by
    # one HTTP test can't leak into another's shutdown (which would try to .kill() it).
    _runner.manager._browsers.clear()
    _runner.manager._closed = False
    # The manifest path is redirected per-test (above); reset the content-diff cache too,
    # or _save_manifest would think "unchanged" and skip writing to the new tmp path.
    _runner.manager._last_manifest_json = None
    _runner._init_done.set()
    yield
    _runner._init_done.clear()
