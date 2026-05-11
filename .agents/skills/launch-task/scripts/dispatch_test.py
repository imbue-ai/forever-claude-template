"""Tests for ``dispatch.py``.

Run via: ``uv run pytest .agents/skills/launch-task/scripts/dispatch_test.py``

The tests inject a recording ``Runner`` so no real ``mngr`` / ``tk``
processes are spawned. We assert on (a) the exact argv lists dispatch.py
hands to subprocess (so the lifecycle contract with ``mngr`` cannot drift
silently), (b) pre-flight validation, (c) ticket-id persistence, and (d)
graceful behaviour when ``tk`` is missing or fails.
"""

from __future__ import annotations

import importlib.util
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import pytest

_SCRIPT = Path(__file__).parent / "dispatch.py"
_spec = importlib.util.spec_from_file_location("dispatch", _SCRIPT)
assert _spec is not None and _spec.loader is not None
dispatch_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dispatch_mod)


@dataclass
class _RecordedCall:
    argv: list[str]
    kwargs: dict[str, Any]


@dataclass
class _StubResult:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class _RecordingRunner(dispatch_mod.Runner):  # type: ignore[name-defined]
    """Records every ``run`` call; returns canned results keyed by argv prefix."""

    calls: list[_RecordedCall] = field(default_factory=list)
    # Map first-2-tokens tuple ("tk","create") -> result or callable raising.
    _responses: dict[tuple[str, ...], Any] = field(default_factory=dict)

    def respond(self, prefix: tuple[str, ...], result: Any) -> None:
        self._responses[prefix] = result

    def run(self, argv: Sequence[str], **kwargs):  # type: ignore[override]
        argv_list = list(argv)
        self.calls.append(_RecordedCall(argv=argv_list, kwargs=kwargs))
        key = tuple(argv_list[:2])
        canned = self._responses.get(key, _StubResult())
        if isinstance(canned, BaseException):
            raise canned
        return canned


def _make_layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create runtime_dir / task_file / extra_dir under tmp_path."""
    runtime = tmp_path / "runtime" / "launch-task" / "demo"
    runtime.mkdir(parents=True)
    task = runtime / "task.md"
    task.write_text("---\nlead_agent: lead\n---\n\nbody\n")
    extra = tmp_path / "runtime" / "do-something-new" / "demo"
    extra.mkdir(parents=True)
    (extra / "sample.json").write_text("{}")
    return runtime, task, extra


def test_happy_path_no_ticket_no_extras(tmp_path: Path) -> None:
    runtime, task, _ = _make_layout(tmp_path)
    runner = _RecordingRunner()

    rc = dispatch_mod.dispatch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        extra_pushes=(),
        workspace="ws-1",
        ticket_title=None,
        ticket_acceptance="acc",
        runner=runner,
    )

    assert rc == 0
    argvs = [c.argv for c in runner.calls]
    assert argvs == [
        ["mngr", "create", "demo-worker", "-t", "worker", "--label", "workspace=ws-1"],
        [
            "mngr",
            "push",
            f"demo-worker:{runtime}/",
            "--source",
            f"{runtime}/",
            "--uncommitted-changes=merge",
        ],
        ["mngr", "message", "demo-worker", "--message-file", str(task)],
    ]
    assert not (runtime / "ticket_id.txt").exists()


def test_extra_push_dirs_are_pushed_after_runtime(tmp_path: Path) -> None:
    runtime, task, extra = _make_layout(tmp_path)
    runner = _RecordingRunner()

    rc = dispatch_mod.dispatch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        extra_pushes=(extra,),
        workspace="ws-1",
        ticket_title=None,
        ticket_acceptance="acc",
        runner=runner,
    )

    assert rc == 0
    push_calls = [c.argv for c in runner.calls if c.argv[:2] == ["mngr", "push"]]
    assert push_calls == [
        [
            "mngr",
            "push",
            f"demo-worker:{runtime}/",
            "--source",
            f"{runtime}/",
            "--uncommitted-changes=merge",
        ],
        [
            "mngr",
            "push",
            f"demo-worker:{extra}/",
            "--source",
            f"{extra}/",
            "--uncommitted-changes=merge",
        ],
    ]


def test_runtime_dir_must_exist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, task, _ = _make_layout(tmp_path)
    runner = _RecordingRunner()
    rc = dispatch_mod.dispatch(
        name="demo-worker",
        template="worker",
        runtime_dir=tmp_path / "missing",
        task_file=task,
        extra_pushes=(),
        workspace="ws",
        ticket_title=None,
        ticket_acceptance="",
        runner=runner,
    )
    assert rc == 2
    assert runner.calls == []
    assert "runtime-dir" in capsys.readouterr().err


def test_task_file_must_exist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime, _, _ = _make_layout(tmp_path)
    runner = _RecordingRunner()
    rc = dispatch_mod.dispatch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=runtime / "missing.md",
        extra_pushes=(),
        workspace="ws",
        ticket_title=None,
        ticket_acceptance="",
        runner=runner,
    )
    assert rc == 2
    assert runner.calls == []
    assert "task-file" in capsys.readouterr().err


def test_extra_push_must_exist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime, task, _ = _make_layout(tmp_path)
    runner = _RecordingRunner()
    rc = dispatch_mod.dispatch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        extra_pushes=(tmp_path / "missing",),
        workspace="ws",
        ticket_title=None,
        ticket_acceptance="",
        runner=runner,
    )
    assert rc == 2
    assert runner.calls == []
    assert "extra-push" in capsys.readouterr().err


def test_ticket_happy_path_writes_ticket_id_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, task, _ = _make_layout(tmp_path)
    monkeypatch.setattr(dispatch_mod.shutil, "which", lambda _name: "/usr/bin/tk")
    runner = _RecordingRunner()
    runner.respond(("tk", "create"), _StubResult(stdout="\nT-42\n"))
    runner.respond(("tk", "start"), _StubResult())

    rc = dispatch_mod.dispatch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        extra_pushes=(),
        workspace="ws",
        ticket_title="demo ticket",
        ticket_acceptance="acc",
        runner=runner,
    )

    assert rc == 0
    ticket_file = runtime / "ticket_id.txt"
    assert ticket_file.read_text() == "T-42\n"
    # tk create was invoked with title and acceptance, then tk start with the ID.
    tk_calls = [c.argv for c in runner.calls if c.argv[0] == "tk"]
    assert tk_calls == [
        ["tk", "create", "demo ticket", "-t", "task", "--acceptance", "acc"],
        ["tk", "start", "T-42"],
    ]


def test_ticket_skipped_when_tk_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime, task, _ = _make_layout(tmp_path)
    monkeypatch.setattr(dispatch_mod.shutil, "which", lambda _name: None)
    runner = _RecordingRunner()

    rc = dispatch_mod.dispatch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        extra_pushes=(),
        workspace="ws",
        ticket_title="demo ticket",
        ticket_acceptance="acc",
        runner=runner,
    )

    assert rc == 0
    assert not (runtime / "ticket_id.txt").exists()
    assert all(c.argv[0] != "tk" for c in runner.calls)
    err = capsys.readouterr().err
    assert "tk not on PATH" in err


def test_ticket_create_failure_is_visible_and_nonfatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime, task, _ = _make_layout(tmp_path)
    monkeypatch.setattr(dispatch_mod.shutil, "which", lambda _name: "/usr/bin/tk")
    runner = _RecordingRunner()
    runner.respond(
        ("tk", "create"),
        subprocess.CalledProcessError(returncode=3, cmd=["tk"], stderr="db locked"),
    )

    rc = dispatch_mod.dispatch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        extra_pushes=(),
        workspace="ws",
        ticket_title="demo ticket",
        ticket_acceptance="acc",
        runner=runner,
    )

    assert rc == 0
    assert not (runtime / "ticket_id.txt").exists()
    err = capsys.readouterr().err
    assert "tk create failed" in err
    assert "db locked" in err
    # mngr still ran despite the tk failure.
    assert any(c.argv[:2] == ["mngr", "create"] for c in runner.calls)


def test_mngr_failure_is_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, task, _ = _make_layout(tmp_path)
    runner = _RecordingRunner()
    runner.respond(
        ("mngr", "create"),
        subprocess.CalledProcessError(returncode=1, cmd=["mngr"]),
    )
    with pytest.raises(subprocess.CalledProcessError):
        dispatch_mod.dispatch(
            name="demo-worker",
            template="worker",
            runtime_dir=runtime,
            task_file=task,
            extra_pushes=(),
            workspace="ws",
            ticket_title=None,
            ticket_acceptance="",
            runner=runner,
        )


def test_main_uses_workspace_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, task, _ = _make_layout(tmp_path)
    captured: list[list[str]] = []

    class _Capture(dispatch_mod.Runner):  # type: ignore[name-defined]
        def run(self, argv, **kwargs):
            captured.append(list(argv))
            return _StubResult()

    monkeypatch.setattr(dispatch_mod, "Runner", _Capture)
    monkeypatch.setenv("MINDS_WORKSPACE_NAME", "alpha")

    rc = dispatch_mod.main(
        [
            "--name",
            "x",
            "--template",
            "worker",
            "--runtime-dir",
            str(runtime),
            "--task-file",
            str(task),
        ]
    )
    assert rc == 0
    create_calls = [c for c in captured if c[:2] == ["mngr", "create"]]
    assert create_calls, captured
    assert "workspace=alpha" in create_calls[0]


def test_main_workspace_defaults_when_env_unset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, task, _ = _make_layout(tmp_path)
    captured: list[list[str]] = []

    class _Capture(dispatch_mod.Runner):  # type: ignore[name-defined]
        def run(self, argv, **kwargs):
            captured.append(list(argv))
            return _StubResult()

    monkeypatch.setattr(dispatch_mod, "Runner", _Capture)
    monkeypatch.delenv("MINDS_WORKSPACE_NAME", raising=False)

    rc = dispatch_mod.main(
        [
            "--name",
            "x",
            "--template",
            "worker",
            "--runtime-dir",
            str(runtime),
            "--task-file",
            str(task),
        ]
    )
    assert rc == 0
    create_calls = [c for c in captured if c[:2] == ["mngr", "create"]]
    assert "workspace=default" in create_calls[0]
