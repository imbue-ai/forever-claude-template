"""Tests for ``reveal_system_interface.py``.

Run via: ``uv run pytest .agents/skills/update-system-interface/scripts/reveal_system_interface_test.py``

The orchestration tests inject a recording ``Runner`` (so no real
``git``/``npm``/``uv``/``mngr`` runs), a programmable ``HttpClient`` (so the
health probe is deterministic), a fake ``Spawner`` (so no throwaway server is
launched), and a no-op sleeper. We assert on the exact commands the reveal hands
to subprocess and on the failure-then-rollback control flow -- the part that must
never regress, because a broken backend takes down the user's whole UI.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

import pytest

_SCRIPT = Path(__file__).parent / "reveal_system_interface.py"
_spec = importlib.util.spec_from_file_location("reveal_system_interface", _SCRIPT)
assert _spec is not None and _spec.loader is not None
reveal_mod = importlib.util.module_from_spec(_spec)
# Register before exec so the module's own dataclasses can resolve __module__.
sys.modules[_spec.name] = reveal_mod
_spec.loader.exec_module(reveal_mod)


def _load_module(name: str):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parent / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


wrapper_mod = _load_module("preview_wrapper_server")

_REPO = Path("/repo")
_ROLLBACK = "abc123def456"
_LIVE_BASE = "http://test-live"


@dataclass
class _Result:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class _RecordingRunner(reveal_mod.Runner):
    """Records every ``run`` call; returns canned results keyed by argv prefix.

    A response may be a single ``_Result`` or a list consumed in order (the last
    entry repeats) -- used to make a command fail once then succeed on retry.
    """

    calls: list[list[str]] = field(default_factory=list)
    _responses: dict[tuple[str, ...], object] = field(default_factory=dict)

    def respond(self, prefix: tuple[str, ...], result: object) -> None:
        self._responses[prefix] = result

    def run(self, argv: Sequence[str], **kwargs) -> _Result:
        argv_list = list(argv)
        self.calls.append(argv_list)
        for prefix, result in self._responses.items():
            if tuple(argv_list[: len(prefix)]) == prefix:
                if isinstance(result, list):
                    result = result.pop(0) if len(result) > 1 else result[0]
                # A canned exception models a command that raises (e.g. a missing
                # binary -> FileNotFoundError) rather than exiting non-zero.
                if isinstance(result, BaseException):
                    raise result
                assert isinstance(result, _Result)
                return result
        return _Result()

    def argvs_starting(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls if tuple(c[: len(prefix)]) == prefix]

    def ran(self, *prefix: str) -> bool:
        return bool(self.argvs_starting(*prefix))


class _FakeHttp(reveal_mod.HttpClient):
    """Returns whatever ``responder(url)`` yields for GETs; records POSTs."""

    def __init__(self, responder: Callable[[str], int | None]) -> None:
        self._responder = responder
        self.get_urls: list[str] = []
        self.post_urls: list[str] = []

    def get_status(self, url: str, timeout: float) -> int | None:
        self.get_urls.append(url)
        return self._responder(url)

    def post_json(
        self, url: str, payload: dict, headers: dict, timeout: float
    ) -> int | None:
        self.post_urls.append(url)
        return 200


@dataclass
class _FakeSpawned:
    terminated: bool = False

    def terminate(self) -> None:
        self.terminated = True


@dataclass
class _FakeSpawner(reveal_mod.Spawner):
    spawns: list[list[str]] = field(default_factory=list)
    detached_spawns: list[list[str]] = field(default_factory=list)
    detached_envs: list[dict] = field(default_factory=list)
    detached_cwds: list[str] = field(default_factory=list)
    detached_pid: int = 4242
    detached_raises: BaseException | None = None
    last: _FakeSpawned | None = None
    detached_pids: list[int] = field(default_factory=list)

    def spawn(self, argv: Sequence[str], cwd: str, env: dict) -> _FakeSpawned:
        self.spawns.append(list(argv))
        self.last = _FakeSpawned()
        return self.last

    def spawn_detached(
        self, argv: Sequence[str], cwd: str, env: dict, log_path: str | None = None
    ) -> int:
        self.detached_spawns.append(list(argv))
        self.detached_envs.append(dict(env))
        self.detached_cwds.append(cwd)
        # Model a boot that fails by raising (e.g. a missing ``uv`` binary).
        if self.detached_raises is not None:
            raise self.detached_raises
        # A preview spawns two servers (inner app + wrapper); hand out a distinct
        # pid per call so tests can confirm both are tracked and torn down.
        pid = self.detached_pid + len(self.detached_pids)
        self.detached_pids.append(pid)
        return pid


def _runner_with_diff(name_status: str, *, dirty: bool = False) -> _RecordingRunner:
    runner = _RecordingRunner()
    runner.respond(
        ("git", "status", "--porcelain"), _Result(stdout=" M foo\n" if dirty else "")
    )
    runner.respond(("git", "diff"), _Result(stdout=name_status))
    return runner


def _reveal(runner: _RecordingRunner, http: _FakeHttp, spawner: _FakeSpawner) -> int:
    return reveal_mod.reveal(
        _ROLLBACK,
        _REPO,
        runner=runner,
        http=http,
        spawner=spawner,
        sleeper=lambda _seconds: None,
        base_url=_LIVE_BASE,
    )


def _all_healthy(_url: str) -> int:
    return 200


def _is_live(url: str) -> bool:
    return url.startswith(_LIVE_BASE)


# --- classification ---------------------------------------------------------


def test_classify_distinguishes_all_four_kinds() -> None:
    changes = reveal_mod.classify_changes(
        [
            "apps/system_interface/frontend/src/views/Chat.ts",
            "apps/system_interface/frontend/package.json",
            "apps/system_interface/imbue/system_interface/server.py",
            "apps/system_interface/pyproject.toml",
        ]
    )
    assert (
        changes.frontend_src,
        changes.frontend_manifest,
        changes.backend_src,
        changes.backend_manifest,
    ) == (
        True,
        True,
        True,
        True,
    )


def test_classify_treats_root_uv_lock_as_backend_manifest() -> None:
    changes = reveal_mod.classify_changes(["uv.lock"])
    assert changes.backend_manifest and changes.backend and not changes.frontend


def test_classify_ignores_backend_test_files() -> None:
    changes = reveal_mod.classify_changes(
        [
            "apps/system_interface/imbue/system_interface/server_test.py",
            "apps/system_interface/imbue/system_interface/test_e2e.py",
        ]
    )
    assert not changes.any


def test_classify_ignores_unrelated_paths() -> None:
    changes = reveal_mod.classify_changes(["README.md", "vendor/mngr/libs/mngr/x.py"])
    assert not changes.any


# --- happy paths ------------------------------------------------------------


def test_frontend_only_builds_and_broadcasts_without_restart() -> None:
    runner = _runner_with_diff("M\tapps/system_interface/frontend/src/views/Chat.ts\n")
    http = _FakeHttp(_all_healthy)
    spawner = _FakeSpawner()

    code = _reveal(runner, http, spawner)

    assert code == 0
    assert runner.ran("npm", "run", "build")
    assert not runner.ran("mngr", "start")  # frontend change never restarts the backend
    assert not runner.ran(
        "uv", "tool", "install"
    )  # no manifest change -> no dep refresh
    assert not spawner.spawns  # no pre-flight for a frontend-only change
    assert http.post_urls  # reload broadcast sent


def test_backend_with_manifest_refreshes_preflights_restarts_and_probes() -> None:
    runner = _runner_with_diff(
        "M\tapps/system_interface/imbue/system_interface/server.py\nM\tapps/system_interface/pyproject.toml\n"
    )
    http = _FakeHttp(_all_healthy)
    spawner = _FakeSpawner()

    code = _reveal(runner, http, spawner)

    assert code == 0
    assert runner.argvs_starting("uv", "tool", "install")[0] == [
        "uv",
        "tool",
        "install",
        "-e",
        "apps/system_interface",
        "--reinstall",
    ]
    assert spawner.spawns and spawner.spawns[0] == [
        reveal_mod.TOOL_NAME
    ]  # pre-flight booted
    assert spawner.last is not None and spawner.last.terminated  # and torn down
    assert runner.ran("mngr", "start", "--restart", "system-services")
    assert any(_is_live(u) for u in http.get_urls)  # live health probed


def test_backend_src_only_skips_dependency_refresh() -> None:
    runner = _runner_with_diff(
        "M\tapps/system_interface/imbue/system_interface/server.py\n"
    )
    http = _FakeHttp(_all_healthy)

    code = _reveal(runner, http, _FakeSpawner())

    assert code == 0
    assert not runner.ran("uv", "tool", "install")
    assert runner.ran("mngr", "start")


def test_no_relevant_changes_does_nothing() -> None:
    runner = _runner_with_diff("M\tREADME.md\n")
    http = _FakeHttp(_all_healthy)

    code = _reveal(runner, http, _FakeSpawner())

    assert code == 0
    assert not runner.ran("npm", "run", "build")
    assert not runner.ran("mngr", "start")


# --- failure + autonomous rollback ------------------------------------------


def test_failed_preflight_never_restarts_live_service_and_rolls_back() -> None:
    # New backend file that cannot boot: pre-flight (non-live URL) never returns
    # 200; live URL is healthy (old code still running, and healthy after revert).
    runner = _runner_with_diff(
        "A\tapps/system_interface/imbue/system_interface/new_module.py\n"
    )
    http = _FakeHttp(lambda url: 200 if _is_live(url) else None)

    code = _reveal(runner, http, _FakeSpawner())

    assert code == 2  # rolled back, UI healthy
    # The live service was never restarted -- pre-flight failed before the
    # restart, so the running service is still healthy on known-good code and
    # recovery must NOT restart it (doing so would needlessly blip a live UI).
    assert not runner.ran("mngr", "start")
    # Recovery still re-confirmed the untouched service via the health probe.
    assert any(_is_live(u) for u in http.get_urls)
    # An added file is removed on rollback (not checked out).
    assert runner.ran("git", "rm", "--force", "--ignore-unmatch")
    assert not runner.ran("git", "checkout", _ROLLBACK)


def test_failed_preflight_with_manifest_refreshes_deps_but_does_not_restart() -> None:
    # A backend manifest change whose merged code fails pre-flight. Recovery must
    # still reinstall deps back to known-good (to fix the on-disk venv) but must
    # NOT restart the live service, which was never touched.
    runner = _runner_with_diff(
        "M\tapps/system_interface/pyproject.toml\nM\tapps/system_interface/imbue/system_interface/server.py\n"
    )
    http = _FakeHttp(lambda url: 200 if _is_live(url) else None)

    code = _reveal(runner, http, _FakeSpawner())

    assert code == 2
    assert not runner.ran("mngr", "start")  # untouched live service is not restarted
    # uv tool install ran twice: once in the failed reveal, once in recovery to
    # restore the known-good dependency set on disk.
    assert len(runner.argvs_starting("uv", "tool", "install")) == 2


def test_failed_post_restart_health_triggers_rollback_then_recovers() -> None:
    # Pre-flight passes, but the live service stays unhealthy after the first
    # restart and only recovers after the rollback's restart. Key the health off
    # how many restarts have happened (wait_healthy retries many times, so a
    # short None sequence would otherwise pass on a later poll).
    runner = _runner_with_diff(
        "M\tapps/system_interface/imbue/system_interface/server.py\n"
    )

    def responder(url: str) -> int | None:
        if not _is_live(url):
            return 200  # pre-flight always boots
        restarts = runner.calls.count(["mngr", "start", "--restart", "system-services"])
        return 200 if restarts >= 2 else None

    http = _FakeHttp(responder)

    code = _reveal(runner, http, _FakeSpawner())

    assert code == 2
    assert runner.ran(
        "git", "checkout", _ROLLBACK
    )  # modified file restored from known-good
    assert (
        len(
            [
                c
                for c in runner.calls
                if c == ["mngr", "start", "--restart", "system-services"]
            ]
        )
        == 2
    )


def test_emergency_when_rollback_cannot_restore_health() -> None:
    runner = _runner_with_diff(
        "M\tapps/system_interface/imbue/system_interface/server.py\n"
    )
    http = _FakeHttp(
        lambda url: None if _is_live(url) else 200
    )  # live never healthy, even after revert

    code = _reveal(runner, http, _FakeSpawner())

    assert code == 3


def test_frontend_build_failure_rolls_back() -> None:
    runner = _runner_with_diff("M\tapps/system_interface/frontend/src/views/Chat.ts\n")
    # First build (the reveal) fails; the recovery rebuild from known-good succeeds.
    runner.respond(
        ("npm", "run", "build"), [_Result(returncode=1, stderr="type error"), _Result()]
    )
    http = _FakeHttp(_all_healthy)

    code = _reveal(runner, http, _FakeSpawner())

    # First build fails -> rollback -> recovery rebuild (default success) -> healthy serve probe.
    assert code == 2
    assert runner.ran("git", "checkout", _ROLLBACK)


# --- preconditions ----------------------------------------------------------


def test_dirty_tree_refuses_before_touching_anything() -> None:
    runner = _runner_with_diff(
        "M\tapps/system_interface/imbue/system_interface/server.py\n", dirty=True
    )
    http = _FakeHttp(_all_healthy)

    with pytest.raises(reveal_mod.PreconditionError):
        _reveal(runner, http, _FakeSpawner())

    assert not runner.ran("mngr", "start")
    assert not runner.ran("npm", "run", "build")


def test_main_maps_precondition_to_exit_1(tmp_path: Path) -> None:
    # main() wires real deps; point it at an empty dir so the first git call
    # (status) fails as a CalledProcessError -> exit 1, proving the mapping
    # without needing a real repo.
    code = reveal_mod.main(
        ["reveal", "--rollback-to", _ROLLBACK, "--repo-root", str(tmp_path)]
    )
    assert code == 1


# --- tree restoration -------------------------------------------------------


def test_restore_tree_removes_adds_and_checks_out_the_rest() -> None:
    runner = _RecordingRunner()
    reveal_mod._restore_tree(
        [
            ("A", "apps/system_interface/imbue/system_interface/new_module.py"),
            ("M", "apps/system_interface/imbue/system_interface/server.py"),
            ("D", "apps/system_interface/frontend/src/old.ts"),
        ],
        _ROLLBACK,
        _REPO,
        runner,
    )
    assert runner.argvs_starting("git", "rm") == [
        [
            "git",
            "rm",
            "--force",
            "--ignore-unmatch",
            "apps/system_interface/imbue/system_interface/new_module.py",
        ]
    ]
    checkouts = [c[-1] for c in runner.argvs_starting("git", "checkout")]
    assert checkouts == [
        "apps/system_interface/imbue/system_interface/server.py",
        "apps/system_interface/frontend/src/old.ts",
    ]


# --- preview setup ----------------------------------------------------------


_SLUG = "demo-change"


def _make_work_dir(tmp_path: Path) -> Path:
    """A stand-in for a worker's work_dir: a folder with an apps/system_interface."""
    work_dir = tmp_path / "worker"
    (work_dir / reveal_mod.APP_DIR).mkdir(parents=True)
    return work_dir


def _preview(
    runner: _RecordingRunner,
    http: _FakeHttp,
    spawner: _FakeSpawner,
    repo_root: Path,
    work_dir: Path,
) -> int:
    return reveal_mod.preview(
        _SLUG,
        str(work_dir),
        repo_root,
        runner=runner,
        http=http,
        spawner=spawner,
        sleeper=lambda _seconds: None,
    )


def _state_path(repo_root: Path) -> Path:
    return reveal_mod._preview_state_path(repo_root, _SLUG)


def test_preview_boots_the_work_dir_registers_and_records_state(tmp_path: Path) -> None:
    work_dir = _make_work_dir(tmp_path)
    runner = _RecordingRunner()
    spawner = _FakeSpawner()

    code = _preview(runner, _FakeHttp(_all_healthy), spawner, tmp_path, work_dir)

    assert code == 0
    # No re-clone / rebuild: the worker already built its work_dir.
    assert not runner.ran("git", "fetch")
    assert not runner.ran("git", "worktree", "add")
    assert not runner.ran("uv", "sync")
    assert not runner.ran("npm", "run", "build")
    # Booted two detached servers: the worker's instance (cwd inside its work_dir),
    # then the wrapper chrome page that embeds it.
    assert spawner.detached_spawns[0] == ["uv", "run", reveal_mod.TOOL_NAME]
    assert spawner.detached_cwds[0] == str(work_dir / reveal_mod.APP_DIR)
    wrapper_argv = spawner.detached_spawns[1]
    assert reveal_mod.PREVIEW_WRAPPER_SCRIPT in wrapper_argv[1]
    assert "--inner-service" in wrapper_argv
    assert reveal_mod.PREVIEW_INNER_SERVICE_NAME in wrapper_argv
    # Layout persistence is neutered (no MNGR_AGENT_ID) but discovery is kept.
    env = spawner.detached_envs[0]
    assert "MNGR_AGENT_ID" not in env
    assert env["SYSTEM_INTERFACE_HOST"] == "127.0.0.1"
    assert env["SYSTEM_INTERFACE_PORT"]
    # Registered both the inner app and the user-facing wrapper as proxied services
    # (each ``forward_port ... --name <name> --url <url>`` argv carries the name).
    registered = runner.argvs_starting(*reveal_mod.FORWARD_PORT_CMD, "--name")
    flat = [token for argv in registered for token in argv]
    assert reveal_mod.PREVIEW_INNER_SERVICE_NAME in flat
    assert reveal_mod.PREVIEW_SERVICE_NAME in flat
    # Recorded enough state for unpreview to find both servers + services later.
    state = json.loads(_state_path(tmp_path).read_text())
    assert state["pids"] == spawner.detached_pids
    assert state["services"] == [
        reveal_mod.PREVIEW_INNER_SERVICE_NAME,
        reveal_mod.PREVIEW_SERVICE_NAME,
    ]
    # The user-facing tab to open is the wrapper.
    assert state["service"] == reveal_mod.PREVIEW_SERVICE_NAME
    assert state["work_dir"] == str(work_dir)
    assert isinstance(state["inner_port"], int)
    assert isinstance(state["wrapper_port"], int)


def test_preview_rejects_a_work_dir_without_the_app(tmp_path: Path) -> None:
    # A wrong --work-dir (or a destroyed worker) should fail fast and touch nothing.
    runner = _RecordingRunner()
    spawner = _FakeSpawner()
    bad_work_dir = tmp_path / "gone"  # no apps/system_interface under it

    code = reveal_mod.preview(
        _SLUG,
        str(bad_work_dir),
        tmp_path,
        runner=runner,
        http=_FakeHttp(_all_healthy),
        spawner=spawner,
        sleeper=lambda _seconds: None,
    )

    assert code == 1
    assert not spawner.detached_spawns
    assert not runner.ran("kill")  # didn't disturb any existing preview
    assert not _state_path(tmp_path).exists()


def test_preview_clears_a_stale_preview_before_booting(tmp_path: Path) -> None:
    # A leftover preview from a prior run must be torn down first so the fixed
    # service name and state dir can be reused cleanly.
    work_dir = _make_work_dir(tmp_path)
    state_dir = reveal_mod._preview_state_dir(tmp_path, _SLUG)
    state_dir.mkdir(parents=True)
    _state_path(tmp_path).write_text(
        json.dumps({"pid": 999, "service": reveal_mod.PREVIEW_SERVICE_NAME})
    )
    runner = _RecordingRunner()

    code = _preview(runner, _FakeHttp(_all_healthy), _FakeSpawner(), tmp_path, work_dir)

    assert code == 0
    assert runner.ran("kill", "-TERM", "-999")  # old server killed
    assert runner.ran(
        *reveal_mod.FORWARD_PORT_CMD, "--remove", "--name"
    )  # old service deregistered
    # A fresh state file replaced the stale one (inner app pid recorded first).
    assert json.loads(_state_path(tmp_path).read_text())["pids"][0] == 4242


def test_preview_tears_down_when_the_boot_raises(tmp_path: Path) -> None:
    # The boot can fail by raising (a missing ``uv`` binary surfaces as
    # FileNotFoundError) rather than exiting non-zero -- teardown must still run.
    work_dir = _make_work_dir(tmp_path)
    runner = _RecordingRunner()
    spawner = _FakeSpawner(detached_raises=FileNotFoundError("uv not found"))

    code = _preview(runner, _FakeHttp(_all_healthy), spawner, tmp_path, work_dir)

    assert code == 1
    assert not runner.ran(*reveal_mod.FORWARD_PORT_CMD, "--name")  # never registered
    assert not _state_path(tmp_path).exists()  # no state left behind


def test_preview_tears_down_booted_server_when_it_never_gets_healthy(
    tmp_path: Path,
) -> None:
    work_dir = _make_work_dir(tmp_path)
    runner = _RecordingRunner()
    spawner = _FakeSpawner()
    # The preview port never returns 200.
    http = _FakeHttp(lambda _url: None)

    code = _preview(runner, http, spawner, tmp_path, work_dir)

    assert code == 1
    assert spawner.detached_spawns  # it was booted
    assert runner.ran("kill", "-TERM", f"-{spawner.detached_pid}")  # then killed
    assert not runner.ran(
        *reveal_mod.FORWARD_PORT_CMD, "--name"
    )  # never registered (health failed first)
    assert not _state_path(tmp_path).exists()


def test_preview_tears_down_both_servers_when_the_wrapper_never_gets_healthy(
    tmp_path: Path,
) -> None:
    # The inner app boots and registers fine, but the wrapper page never returns
    # 200 -- teardown must unwind BOTH servers and the already-registered inner
    # service, leaving no partial state behind.
    work_dir = _make_work_dir(tmp_path)
    runner = _RecordingRunner()
    spawner = _FakeSpawner()
    # Inner health (``/api/agents``) passes; the wrapper root probe never does.
    http = _FakeHttp(lambda url: 200 if reveal_mod.HEALTH_PATH in url else None)

    code = _preview(runner, http, spawner, tmp_path, work_dir)

    assert code == 1
    assert len(spawner.detached_pids) == 2  # both servers were booted
    for pid in spawner.detached_pids:
        assert runner.ran("kill", "-TERM", f"-{pid}")  # both killed
    # The inner service was registered, so teardown must deregister it.
    assert runner.ran(*reveal_mod.FORWARD_PORT_CMD, "--remove", "--name")
    assert not _state_path(tmp_path).exists()


# --- wrapper page -----------------------------------------------------------


def test_wrapper_page_survives_the_dispatcher_html_rewriter() -> None:
    # The wrapper page is served *through* the dispatcher's proxy, which rewrites
    # absolute-path ``src=``/``href=`` attributes to prepend the wrapper's own
    # service prefix. The inner iframe URL must therefore NOT appear as a static
    # ``src="/..."`` attribute, or it would be rewritten to point back at the
    # wrapper instead of the inner service. This runs the *real* rewriter to lock
    # that contract in -- if someone "simplifies" the page to a static src, the
    # double-prefix assertion below fails.
    from imbue.system_interface.primitives import ServiceName
    from imbue.system_interface.proxy import rewrite_proxied_html

    html = wrapper_mod.build_wrapper_html(
        inner_service=reveal_mod.PREVIEW_INNER_SERVICE_NAME,
        title="demo-change",
    )
    rewritten = rewrite_proxied_html(html, ServiceName(reveal_mod.PREVIEW_SERVICE_NAME))

    # The pieces the page concatenates into the inner URL at runtime are intact...
    assert '"/service/"' in rewritten
    assert reveal_mod.PREVIEW_INNER_SERVICE_NAME in rewritten
    # ...and nothing was rewritten into a wrapper-prefixed inner path.
    double_prefix = f"/service/{reveal_mod.PREVIEW_SERVICE_NAME}/service/"
    assert double_prefix not in rewritten


def test_wrapper_page_escapes_the_title() -> None:
    html = wrapper_mod.build_wrapper_html(inner_service="svc", title='<b>x</b> & "y"')
    assert "<b>x</b>" not in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html


# --- preview teardown -------------------------------------------------------


def test_unpreview_tears_down_both_servers_and_services(tmp_path: Path) -> None:
    state_dir = reveal_mod._preview_state_dir(tmp_path, _SLUG)
    state_dir.mkdir(parents=True)
    _state_path(tmp_path).write_text(
        json.dumps(
            {
                "pids": [4242, 4243],
                "services": [
                    reveal_mod.PREVIEW_INNER_SERVICE_NAME,
                    reveal_mod.PREVIEW_SERVICE_NAME,
                ],
            }
        )
    )
    runner = _RecordingRunner()

    code = reveal_mod.unpreview(_SLUG, tmp_path, runner=runner)

    assert code == 0
    # Both detached servers killed and both proxied services deregistered.
    assert runner.ran("kill", "-TERM", "-4242")
    assert runner.ran("kill", "-TERM", "-4243")
    removed = [argv[-1] for argv in runner.argvs_starting(*reveal_mod.FORWARD_PORT_CMD, "--remove", "--name")]
    assert reveal_mod.PREVIEW_INNER_SERVICE_NAME in removed
    assert reveal_mod.PREVIEW_SERVICE_NAME in removed
    # The preview served the worker's work_dir in place -- there is no worktree.
    assert not runner.ran("git", "worktree", "remove")
    assert not state_dir.exists()  # state directory deleted


def test_unpreview_tears_down_a_legacy_single_server_state(tmp_path: Path) -> None:
    # A preview recorded before the wrapper used single ``pid``/``service`` keys;
    # unpreview must still tear it down so a stale preview can always be cleaned up.
    state_dir = reveal_mod._preview_state_dir(tmp_path, _SLUG)
    state_dir.mkdir(parents=True)
    _state_path(tmp_path).write_text(
        json.dumps({"pid": 4242, "service": reveal_mod.PREVIEW_SERVICE_NAME})
    )
    runner = _RecordingRunner()

    code = reveal_mod.unpreview(_SLUG, tmp_path, runner=runner)

    assert code == 0
    assert runner.ran("kill", "-TERM", "-4242")
    remove = runner.argvs_starting(*reveal_mod.FORWARD_PORT_CMD, "--remove", "--name")
    assert remove and remove[0][-1] == reveal_mod.PREVIEW_SERVICE_NAME
    assert not state_dir.exists()


def test_unpreview_without_state_is_a_noop_success(tmp_path: Path) -> None:
    runner = _RecordingRunner()

    code = reveal_mod.unpreview(_SLUG, tmp_path, runner=runner)

    assert code == 0
    assert not runner.ran("kill")


def test_main_routes_unpreview(tmp_path: Path) -> None:
    # No state present -> idempotent no-op success, proving the subcommand wires
    # through main() and reaches unpreview.
    code = reveal_mod.main(["unpreview", "--slug", _SLUG, "--repo-root", str(tmp_path)])
    assert code == 0
