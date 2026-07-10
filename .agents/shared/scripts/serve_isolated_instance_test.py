"""Tests for ``serve_isolated_instance.py``.

Run via: ``uv run pytest .agents/shared/scripts/serve_isolated_instance_test.py``

Like the ``reveal_system_interface.py`` tests, these inject a recording
``Runner`` (so no real ``uv``/``forward_port`` runs), a programmable
``HttpClient`` (so the health probe is deterministic), a fake ``Spawner`` (so no
throwaway server is launched), and a no-op sleeper. We assert on the exact env
overrides / commands the ``up`` motion produces and on the teardown-on-failure
control flow, which must never regress: a leaked server holds a port and a leaked
service registration routes the live UI at a dead port.
"""

from __future__ import annotations

import importlib.util
import json
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

import pytest

_SCRIPT = Path(__file__).parent / "serve_isolated_instance.py"
_spec = importlib.util.spec_from_file_location("serve_isolated_instance", _SCRIPT)
assert _spec is not None and _spec.loader is not None
mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = mod
_spec.loader.exec_module(mod)


def _load_module(name: str):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).parent / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


wrapper_mod = _load_module("preview_wrapper_server")

_NAME = "demo"
_PORT_ENV = "MYSVC_PORT"
_LAUNCH = ["uv", "run", "my-service"]


@dataclass
class _Result:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class _RecordingRunner(mod.Runner):
    """Records every ``run`` call; returns canned results keyed by argv prefix."""

    calls: list[list[str]] = field(default_factory=list)
    _responses: dict[tuple[str, ...], object] = field(default_factory=dict)
    killed_pgroups: list[int] = field(default_factory=list)

    def respond(self, prefix: tuple[str, ...], result: object) -> None:
        self._responses[prefix] = result

    def run(self, argv: Sequence[str], **kwargs) -> _Result:
        argv_list = list(argv)
        self.calls.append(argv_list)
        for prefix, result in self._responses.items():
            if tuple(argv_list[: len(prefix)]) == prefix:
                assert isinstance(result, _Result)
                return result
        return _Result()

    def argvs_starting(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls if tuple(c[: len(prefix)]) == prefix]

    def ran(self, *prefix: str) -> bool:
        return bool(self.argvs_starting(*prefix))

    def kill_process_group(self, pid: int, sig: int = signal.SIGTERM) -> None:
        self.killed_pgroups.append(pid)

    def killed_pgroup(self, pid: int) -> bool:
        return pid in self.killed_pgroups


class _FakeHttp(mod.HttpClient):
    """Returns whatever ``responder(url)`` yields for GETs."""

    def __init__(self, responder: Callable[[str], int | None]) -> None:
        self._responder = responder
        self.get_urls: list[str] = []

    def get_status(self, url: str, timeout: float) -> int | None:
        self.get_urls.append(url)
        return self._responder(url)


@dataclass
class _FakeSpawner(mod.Spawner):
    detached_spawns: list[list[str]] = field(default_factory=list)
    detached_envs: list[dict] = field(default_factory=list)
    detached_cwds: list[str] = field(default_factory=list)
    detached_pid: int = 4242
    detached_raises: BaseException | None = None
    detached_pids: list[int] = field(default_factory=list)

    def spawn_detached(
        self, argv: Sequence[str], cwd: str, env: dict, log_path: str
    ) -> int:
        self.detached_spawns.append(list(argv))
        self.detached_envs.append(dict(env))
        self.detached_cwds.append(cwd)
        if self.detached_raises is not None:
            raise self.detached_raises
        pid = self.detached_pid + len(self.detached_pids)
        self.detached_pids.append(pid)
        return pid


def _all_healthy(_url: str) -> int:
    return 200


def _up(
    tmp_path: Path,
    *,
    runner: _RecordingRunner | None = None,
    http: _FakeHttp | None = None,
    spawner: _FakeSpawner | None = None,
    cwd: Path | None = None,
    **kwargs,
) -> int:
    return mod.up(
        _NAME,
        _LAUNCH,
        str(cwd if cwd is not None else tmp_path),
        tmp_path,
        port_env=_PORT_ENV,
        runner=runner or _RecordingRunner(),
        http=http or _FakeHttp(_all_healthy),
        spawner=spawner or _FakeSpawner(),
        sleeper=lambda _seconds: None,
        **kwargs,
    )


def _state_path(tmp_path: Path) -> Path:
    return mod._state_path(tmp_path, _NAME)


# --- bare instance (own testing) --------------------------------------------


def test_up_boots_on_a_port_injects_port_env_and_reports_url(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spawner = _FakeSpawner()

    code = _up(tmp_path, spawner=spawner)

    assert code == 0
    # Launched the given command from the given cwd.
    assert spawner.detached_spawns[0] == _LAUNCH
    assert spawner.detached_cwds[0] == str(tmp_path)
    # The chosen free port was injected into the named env var.
    env = spawner.detached_envs[0]
    assert env[_PORT_ENV]
    injected_port = int(env[_PORT_ENV])
    # The loopback URL (with the injected port) is printed to stdout for capture.
    out = capsys.readouterr().out.strip()
    assert out == f"http://127.0.0.1:{injected_port}"
    # State records the single server and no services.
    state = json.loads(_state_path(tmp_path).read_text())
    assert state["pids"] == spawner.detached_pids
    assert state["services"] == []
    assert state["inner_port"] == injected_port


def test_up_bare_instance_registers_no_service(tmp_path: Path) -> None:
    runner = _RecordingRunner()

    code = _up(tmp_path, runner=runner)

    assert code == 0
    assert not runner.ran(*mod.FORWARD_PORT_CMD, "--name")


def test_up_applies_env_overrides_and_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A var present in the ambient env is removed; an override is added.
    monkeypatch.setenv("MNGR_AGENT_ID", "live-agent")
    spawner = _FakeSpawner()

    code = _up(
        tmp_path,
        spawner=spawner,
        env_overrides={"MYSVC_DATA_DIR": "/tmp/scratch"},
        unset_env=["MNGR_AGENT_ID"],
        host_env="MYSVC_HOST",
    )

    assert code == 0
    env = spawner.detached_envs[0]
    assert "MNGR_AGENT_ID" not in env
    assert env["MYSVC_DATA_DIR"] == "/tmp/scratch"
    assert env["MYSVC_HOST"] == "127.0.0.1"


def test_up_rejects_a_missing_cwd(tmp_path: Path) -> None:
    spawner = _FakeSpawner()

    code = _up(tmp_path, spawner=spawner, cwd=tmp_path / "gone")

    assert code == 1
    assert not spawner.detached_spawns
    assert not _state_path(tmp_path).exists()


def test_up_tears_down_when_the_boot_raises(tmp_path: Path) -> None:
    runner = _RecordingRunner()
    spawner = _FakeSpawner(detached_raises=FileNotFoundError("uv not found"))

    code = _up(tmp_path, runner=runner, spawner=spawner)

    assert code == 1
    assert not runner.ran(*mod.FORWARD_PORT_CMD, "--name")
    assert not _state_path(tmp_path).exists()


def test_up_tears_down_when_the_instance_never_gets_healthy(tmp_path: Path) -> None:
    runner = _RecordingRunner()
    spawner = _FakeSpawner()
    http = _FakeHttp(lambda _url: None)  # never returns 200

    code = _up(tmp_path, runner=runner, http=http, spawner=spawner)

    assert code == 1
    assert spawner.detached_spawns  # it was booted
    assert runner.killed_pgroup(spawner.detached_pid)  # then killed
    assert not runner.ran(*mod.FORWARD_PORT_CMD, "--name")  # never registered
    assert not _state_path(tmp_path).exists()


def test_up_clears_a_stale_instance_before_booting(tmp_path: Path) -> None:
    state_dir = mod._state_dir(tmp_path, _NAME)
    state_dir.mkdir(parents=True)
    _state_path(tmp_path).write_text(
        json.dumps({"pids": [999], "services": ["old-svc"]})
    )
    runner = _RecordingRunner()

    code = _up(tmp_path, runner=runner)

    assert code == 0
    assert runner.killed_pgroup(999)  # old server killed
    assert runner.ran(*mod.FORWARD_PORT_CMD, "--remove", "--name")  # old svc removed
    assert json.loads(_state_path(tmp_path).read_text())["pids"][0] == 4242


# --- registered service (surfaced, no wrapper) ------------------------------


def test_up_with_service_name_registers_the_instance(tmp_path: Path) -> None:
    runner = _RecordingRunner()

    code = _up(tmp_path, runner=runner, service_name="demo-app")

    assert code == 0
    registered = runner.argvs_starting(*mod.FORWARD_PORT_CMD, "--name")
    flat = [token for argv in registered for token in argv]
    assert "demo-app" in flat
    state = json.loads(_state_path(tmp_path).read_text())
    assert state["services"] == ["demo-app"]


# --- preview (surfaced, wrapped) --------------------------------------------


def _up_preview(
    tmp_path: Path,
    *,
    runner: _RecordingRunner | None = None,
    http: _FakeHttp | None = None,
    spawner: _FakeSpawner | None = None,
) -> int:
    return _up(
        tmp_path,
        runner=runner,
        http=http,
        spawner=spawner,
        service_name="demo-app",
        preview_service_name="demo-preview",
        preview_title="my change",
    )


def test_up_preview_boots_wrapper_registers_both_and_reports_tab(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = _RecordingRunner()
    spawner = _FakeSpawner()

    code = _up_preview(tmp_path, runner=runner, spawner=spawner)

    assert code == 0
    # Two detached servers: the instance, then the wrapper chrome page.
    assert spawner.detached_spawns[0] == _LAUNCH
    wrapper_argv = spawner.detached_spawns[1]
    assert mod.WRAPPER_SCRIPT in wrapper_argv[1]
    assert "--inner-service" in wrapper_argv
    assert "demo-app" in wrapper_argv
    assert "my change" in wrapper_argv
    # Registered both the inner app and the user-facing wrapper.
    registered = runner.argvs_starting(*mod.FORWARD_PORT_CMD, "--name")
    flat = [token for argv in registered for token in argv]
    assert "demo-app" in flat
    assert "demo-preview" in flat
    # The tab to open (the wrapper's service path) is printed to stdout.
    assert capsys.readouterr().out.strip() == "/service/demo-preview/"
    state = json.loads(_state_path(tmp_path).read_text())
    assert state["pids"] == spawner.detached_pids
    assert state["services"] == ["demo-app", "demo-preview"]
    assert isinstance(state["wrapper_port"], int)


def test_up_preview_requires_service_name(tmp_path: Path) -> None:
    # Preview flags without --service-name is a misconfiguration, not a preview.
    spawner = _FakeSpawner()

    code = _up(
        tmp_path,
        spawner=spawner,
        preview_service_name="demo-preview",
        preview_title="my change",
    )

    assert code == 1
    assert not spawner.detached_spawns  # bailed before booting anything


def test_up_preview_tears_down_both_when_the_wrapper_never_gets_healthy(
    tmp_path: Path,
) -> None:
    runner = _RecordingRunner()
    spawner = _FakeSpawner()
    # Inner health passes; the wrapper root probe never does. The inner is probed
    # on its own port with the caller's health path (``/`` here); the wrapper is
    # probed at ``/``. Distinguish by which port each call targets.
    http = _FakeHttp(lambda url: 200 if _first_port(url, spawner) else None)

    code = _up_preview(tmp_path, runner=runner, http=http, spawner=spawner)

    assert code == 1
    assert len(spawner.detached_pids) == 2  # both booted
    for pid in spawner.detached_pids:
        assert runner.killed_pgroup(pid)  # both killed
    assert runner.ran(*mod.FORWARD_PORT_CMD, "--remove", "--name")  # inner deregistered
    assert not _state_path(tmp_path).exists()


def _first_port(url: str, spawner: _FakeSpawner) -> bool:
    """True iff ``url`` targets the inner instance's port (the first spawn)."""
    inner_port = spawner.detached_envs[0][_PORT_ENV] if spawner.detached_envs else None
    return inner_port is not None and f":{inner_port}" in url


# --- teardown ---------------------------------------------------------------


def test_down_tears_down_servers_and_services(tmp_path: Path) -> None:
    state_dir = mod._state_dir(tmp_path, _NAME)
    state_dir.mkdir(parents=True)
    _state_path(tmp_path).write_text(
        json.dumps({"pids": [4242, 4243], "services": ["demo-app", "demo-preview"]})
    )
    runner = _RecordingRunner()

    code = mod.down(_NAME, tmp_path, runner=runner)

    assert code == 0
    assert runner.killed_pgroup(4242)
    assert runner.killed_pgroup(4243)
    removed = [
        argv[-1]
        for argv in runner.argvs_starting(*mod.FORWARD_PORT_CMD, "--remove", "--name")
    ]
    assert "demo-app" in removed
    assert "demo-preview" in removed
    assert not state_dir.exists()


def test_down_without_state_is_a_noop_success(tmp_path: Path) -> None:
    runner = _RecordingRunner()

    code = mod.down(_NAME, tmp_path, runner=runner)

    assert code == 0
    assert not runner.killed_pgroups


def test_down_reports_unreadable_state(tmp_path: Path) -> None:
    state_dir = mod._state_dir(tmp_path, _NAME)
    state_dir.mkdir(parents=True)
    _state_path(tmp_path).write_text("not json{")
    runner = _RecordingRunner()

    code = mod.down(_NAME, tmp_path, runner=runner)

    assert code == 1


# --- CLI + parsing ----------------------------------------------------------


def test_parse_env_assignments_rejects_a_missing_equals() -> None:
    with pytest.raises(mod.InstanceError):
        mod.parse_env_assignments(["NOPE"])


def test_main_up_strips_the_leading_double_dash(tmp_path: Path) -> None:
    # ``argparse.REMAINDER`` keeps the ``--`` separator; ``main`` must drop it so
    # the launch argv is clean. A missing cwd makes ``up`` exit 1 without spawning,
    # which is enough to prove the wiring reached ``up`` with the right launch.
    code = mod.main(
        [
            "up",
            "--name",
            _NAME,
            "--cwd",
            str(tmp_path / "gone"),
            "--port-env",
            _PORT_ENV,
            "--repo-root",
            str(tmp_path),
            "--",
            "uv",
            "run",
            "my-service",
        ]
    )
    assert code == 1  # missing cwd, but it routed through up()


def test_main_routes_down(tmp_path: Path) -> None:
    code = mod.main(["down", "--name", _NAME, "--repo-root", str(tmp_path)])
    assert code == 0  # no state -> idempotent no-op


# --- wrapper page (moved here with the wrapper server) ----------------------


def test_wrapper_page_survives_the_dispatcher_html_rewriter() -> None:
    # The wrapper page is served *through* the system-interface dispatcher's proxy,
    # which rewrites absolute-path ``src=``/``href=`` attributes to prepend the
    # wrapper's own service prefix. The inner iframe URL must therefore NOT appear
    # as a static ``src="/..."`` attribute, or it would be rewritten to point back
    # at the wrapper. This runs the *real* rewriter to lock that contract in.
    from imbue.system_interface.primitives import ServiceName
    from imbue.system_interface.proxy import rewrite_proxied_html

    html = wrapper_mod.build_wrapper_html(inner_service="demo-app", title="my change")
    rewritten = rewrite_proxied_html(html, ServiceName("demo-preview"))

    assert '"/service/"' in rewritten
    assert "demo-app" in rewritten
    assert "/service/demo-preview/service/" not in rewritten


def test_wrapper_page_escapes_the_title() -> None:
    html = wrapper_mod.build_wrapper_html(inner_service="svc", title='<b>x</b> & "y"')
    assert "<b>x</b>" not in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html
