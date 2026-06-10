"""Tests for ``create_worker.py``.

Run via: ``uv run pytest .agents/skills/launch-task/scripts/create_worker_test.py``

The ``launch`` tests inject a recording ``Runner`` so no real ``mngr``
processes are spawned. We assert on (a) the exact argv lists launch hands to
subprocess (so the lifecycle contract with ``mngr`` cannot drift silently)
and (b) pre-flight validation. The ``await`` tests inject a fake clock and
sleeper so the poll loop runs without real time.
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import pytest
from mngr_cli_contract.contract import assert_mngr_argv_valid

_SCRIPT = Path(__file__).parent / "create_worker.py"
_spec = importlib.util.spec_from_file_location("create_worker", _SCRIPT)
assert _spec is not None and _spec.loader is not None
create_worker_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(create_worker_mod)


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
class _RecordingRunner(create_worker_mod.Runner):
    """Records every ``run`` call; returns canned results keyed by argv prefix."""

    calls: list[_RecordedCall] = field(default_factory=list)
    _responses: dict[tuple[str, ...], Any] = field(default_factory=dict)

    def respond(self, prefix: tuple[str, ...], result: Any) -> None:
        self._responses[prefix] = result

    def run(self, argv: Sequence[str], **kwargs):
        argv_list = list(argv)
        self.calls.append(_RecordedCall(argv=argv_list, kwargs=kwargs))
        key = tuple(argv_list[:2])
        canned = self._responses.get(key, _StubResult())
        if isinstance(canned, BaseException):
            raise canned
        return canned


def _make_layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create runtime_dir / task_file / artifacts_dir under tmp_path.

    The task file has plain frontmatter (no ``source_artifacts_dir``); tests
    that exercise the artifacts sync overwrite it via ``_write_task``.
    """
    runtime = tmp_path / "runtime" / "launch-task" / "demo"
    runtime.mkdir(parents=True)
    task = runtime / "task.md"
    task.write_text("---\nlead_agent: lead\n---\n\nbody\n")
    artifacts = tmp_path / "runtime" / "do-something-new" / "demo"
    artifacts.mkdir(parents=True)
    (artifacts / "sample.json").write_text("{}")
    return runtime, task, artifacts


def _write_task(task: Path, source_artifacts_dir: str | None) -> None:
    """Overwrite ``task`` with frontmatter optionally declaring artifacts."""
    fm = "lead_agent: lead\n"
    if source_artifacts_dir is not None:
        fm += f"source_artifacts_dir: {source_artifacts_dir}\n"
    task.write_text(f"---\n{fm}---\n\nbody\n")


def test_happy_path_no_artifacts(tmp_path: Path) -> None:
    runtime, task, _ = _make_layout(tmp_path)
    runner = _RecordingRunner()

    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        workspace="ws-1",
        runner=runner,
    )

    assert rc == 0
    argvs = [c.argv for c in runner.calls]
    assert argvs == [
        ["mngr", "create", "demo-worker", "-t", "worker", "--label", "workspace=ws-1"],
        [
            "mngr",
            "rsync",
            f"{runtime}/",
            f"demo-worker:{runtime}/",
            "--uncommitted-changes=merge",
        ],
        ["mngr", "message", "demo-worker", "--message-file", str(task)],
    ]


def test_source_artifacts_dir_synced_after_runtime(tmp_path: Path) -> None:
    """A frontmatter ``source_artifacts_dir`` is synced right after the runtime dir."""
    runtime, task, artifacts = _make_layout(tmp_path)
    _write_task(task, str(artifacts))
    runner = _RecordingRunner()

    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        workspace="ws-1",
        runner=runner,
    )

    assert rc == 0
    rsync_calls = [c.argv for c in runner.calls if c.argv[:2] == ["mngr", "rsync"]]
    assert rsync_calls == [
        [
            "mngr",
            "rsync",
            f"{runtime}/",
            f"demo-worker:{runtime}/",
            "--uncommitted-changes=merge",
        ],
        [
            "mngr",
            "rsync",
            f"{artifacts}/",
            f"demo-worker:{artifacts}/",
            "--uncommitted-changes=merge",
        ],
    ]


def test_emitted_mngr_argv_accepted_by_live_cli(tmp_path: Path) -> None:
    """Every ``mngr ...`` argv launch actually emits must be accepted by the
    live mngr CLI surface.

    Rather than re-asserting a hand-written expected argv (which mirrors the
    production assumption and so can never catch a divergence when vendor/mngr
    changes its CLI), we take exactly what ``launch`` hands the runner and
    confront it with ``imbue.mngr.main.cli``. It exercises the broadest argv set
    (create + two rsyncs + message) by declaring a ``source_artifacts_dir``.
    """
    runtime, task, artifacts = _make_layout(tmp_path)
    _write_task(task, str(artifacts))
    runner = _RecordingRunner()

    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        workspace="ws-1",
        runner=runner,
    )

    assert rc == 0
    mngr_calls = [c.argv for c in runner.calls if c.argv[:1] == ["mngr"]]
    # Vacuity guard: the full lifecycle is create + two rsyncs + message, so we
    # know the loop below actually validates four real invocations rather than
    # passing on an empty list. This counts steps; it deliberately does NOT pin
    # the subcommand names (that would re-introduce the hand-mirrored
    # expectation this test exists to replace) -- assert_mngr_argv_valid is what
    # confronts each argv with the live CLI.
    assert len(mngr_calls) == 4
    for argv in mngr_calls:
        assert_mngr_argv_valid(argv)


def test_relative_runtime_dir_is_prefixed_for_local_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repo-relative runtime dir is ``./``-prefixed as the local rsync source.

    This is the real launch contract (the skill passes repo-relative paths from
    the repo root). ``mngr rsync`` reads a bare ``runtime/foo/`` as an agent name
    and fails, so the source must be ``./``-prefixed -- while the agent
    destination stays repo-relative so mngr resolves it against the worker's
    workdir rather than the lead's. The absolute-path tests above don't exercise
    this because absolute paths are already recognized as local.
    """
    runtime, task, _ = _make_layout(tmp_path)
    monkeypatch.chdir(tmp_path)
    rel_runtime = runtime.relative_to(tmp_path)
    rel_task = task.relative_to(tmp_path)
    runner = _RecordingRunner()

    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=rel_runtime,
        task_file=rel_task,
        workspace="ws-1",
        runner=runner,
    )

    assert rc == 0
    rsync_calls = [c.argv for c in runner.calls if c.argv[:2] == ["mngr", "rsync"]]
    assert rsync_calls == [
        [
            "mngr",
            "rsync",
            f"./{rel_runtime}/",
            f"demo-worker:{rel_runtime}/",
            "--uncommitted-changes=merge",
        ],
    ]


def test_source_artifacts_dir_missing_is_fatal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A declared but nonexistent ``source_artifacts_dir`` aborts before launch."""
    runtime, task, _ = _make_layout(tmp_path)
    _write_task(task, str(tmp_path / "no-such-dir"))
    runner = _RecordingRunner()

    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        workspace="ws",
        runner=runner,
    )

    assert rc == 2
    assert runner.calls == []
    assert "source_artifacts_dir" in capsys.readouterr().err


def test_source_artifacts_dir_non_string_raises(tmp_path: Path) -> None:
    """A non-string ``source_artifacts_dir`` value raises (full traceback) before
    any mngr call -- a malformed task file is an authoring bug, not a bad CLI arg."""
    runtime, task, _ = _make_layout(tmp_path)
    task.write_text(
        "---\nlead_agent: lead\nsource_artifacts_dir: [a, b]\n---\n\nbody\n"
    )
    runner = _RecordingRunner()

    with pytest.raises(ValueError, match="source_artifacts_dir"):
        create_worker_mod.launch(
            name="demo-worker",
            template="worker",
            runtime_dir=runtime,
            task_file=task,
            workspace="ws",
            runner=runner,
        )

    assert runner.calls == []


def test_invalid_frontmatter_yaml_raises(tmp_path: Path) -> None:
    """A present frontmatter block with invalid YAML raises rather than being
    silently treated as 'no frontmatter' -- it would otherwise mask an
    authoring bug and launch the worker with the wrong inputs."""
    runtime, task, _ = _make_layout(tmp_path)
    # A ``---`` block whose body is not valid YAML (unclosed bracket).
    task.write_text("---\nsource_artifacts_dir: [a, b\n---\n\nbody\n")
    runner = _RecordingRunner()

    with pytest.raises(ValueError, match="invalid YAML"):
        create_worker_mod.launch(
            name="demo-worker",
            template="worker",
            runtime_dir=runtime,
            task_file=task,
            workspace="ws",
            runner=runner,
        )

    assert runner.calls == []


def test_malformed_frontmatter_does_not_abort_launch(tmp_path: Path) -> None:
    """A task file with no/broken frontmatter launches normally with no artifacts
    sync -- frontmatter schema validation is the worker's job, not launch's."""
    runtime, task, _ = _make_layout(tmp_path)
    task.write_text("no frontmatter here, just a body\n")
    runner = _RecordingRunner()

    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        workspace="ws-1",
        runner=runner,
    )

    assert rc == 0
    rsync_calls = [c.argv for c in runner.calls if c.argv[:2] == ["mngr", "rsync"]]
    assert rsync_calls == [
        [
            "mngr",
            "rsync",
            f"{runtime}/",
            f"demo-worker:{runtime}/",
            "--uncommitted-changes=merge",
        ],
    ]


def test_runtime_dir_must_exist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, task, _ = _make_layout(tmp_path)
    runner = _RecordingRunner()
    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=tmp_path / "missing",
        task_file=task,
        workspace="ws",
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
    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=runtime / "missing.md",
        workspace="ws",
        runner=runner,
    )
    assert rc == 2
    assert runner.calls == []
    assert "task-file" in capsys.readouterr().err


def test_mngr_failure_is_fatal(tmp_path: Path) -> None:
    runtime, task, _ = _make_layout(tmp_path)
    runner = _RecordingRunner()
    runner.respond(
        ("mngr", "create"),
        subprocess.CalledProcessError(returncode=1, cmd=["mngr"]),
    )
    with pytest.raises(subprocess.CalledProcessError):
        create_worker_mod.launch(
            name="demo-worker",
            template="worker",
            runtime_dir=runtime,
            task_file=task,
            workspace="ws",
            runner=runner,
        )


def _launch_argv(runtime: Path, task: Path) -> list[str]:
    return [
        "launch",
        "--name",
        "x",
        "--template",
        "worker",
        "--runtime-dir",
        str(runtime),
        "--task-file",
        str(task),
    ]


def test_main_uses_workspace_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, task, _ = _make_layout(tmp_path)
    runner = _RecordingRunner()
    monkeypatch.setenv("MINDS_WORKSPACE_NAME", "alpha")

    rc = create_worker_mod.main(_launch_argv(runtime, task), runner=runner)

    assert rc == 0
    create_calls = [c.argv for c in runner.calls if c.argv[:2] == ["mngr", "create"]]
    assert create_calls, runner.calls
    assert "workspace=alpha" in create_calls[0]


def test_main_workspace_defaults_when_env_unset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, task, _ = _make_layout(tmp_path)
    runner = _RecordingRunner()
    monkeypatch.delenv("MINDS_WORKSPACE_NAME", raising=False)

    rc = create_worker_mod.main(_launch_argv(runtime, task), runner=runner)

    assert rc == 0
    create_calls = [c.argv for c in runner.calls if c.argv[:2] == ["mngr", "create"]]
    assert "workspace=default" in create_calls[0]


def _make_state_dir_with_converter(tmp_path: Path) -> Path:
    """Create a state_dir containing a stub common_transcript.sh."""
    state_dir = tmp_path / "state"
    (state_dir / "commands").mkdir(parents=True)
    script = state_dir / "commands" / "common_transcript.sh"
    script.write_text("#!/usr/bin/env bash\n:\n")
    return state_dir


def test_common_transcript_flushed_before_message_send(tmp_path: Path) -> None:
    """When state_dir has the converter, launch flushes it right before the message."""
    runtime, task, _ = _make_layout(tmp_path)
    state_dir = _make_state_dir_with_converter(tmp_path)
    runner = _RecordingRunner()

    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        workspace="ws-1",
        state_dir=state_dir,
        runner=runner,
    )

    assert rc == 0
    argvs = [c.argv for c in runner.calls]
    expected_script = str(state_dir / "commands" / "common_transcript.sh")
    assert argvs == [
        ["mngr", "create", "demo-worker", "-t", "worker", "--label", "workspace=ws-1"],
        [
            "mngr",
            "rsync",
            f"{runtime}/",
            f"demo-worker:{runtime}/",
            "--uncommitted-changes=merge",
        ],
        [expected_script, "--single-pass"],
        ["mngr", "message", "demo-worker", "--message-file", str(task)],
    ]


def test_common_transcript_skipped_when_state_dir_is_none(tmp_path: Path) -> None:
    """No converter call when state_dir is None (tests / non-mngr envs)."""
    runtime, task, _ = _make_layout(tmp_path)
    runner = _RecordingRunner()

    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        workspace="ws-1",
        state_dir=None,
        runner=runner,
    )

    assert rc == 0
    assert not any(
        "common_transcript.sh" in arg for call in runner.calls for arg in call.argv
    )


def test_common_transcript_skipped_when_script_missing(tmp_path: Path) -> None:
    """No converter call when the script isn't installed (non-claude agents)."""
    runtime, task, _ = _make_layout(tmp_path)
    state_dir = tmp_path / "state-without-converter"
    state_dir.mkdir()
    runner = _RecordingRunner()

    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        workspace="ws-1",
        state_dir=state_dir,
        runner=runner,
    )

    assert rc == 0
    assert not any(
        "common_transcript.sh" in arg for call in runner.calls for arg in call.argv
    )


def test_common_transcript_failure_does_not_abort_launch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A non-zero converter exit must NOT abort launch (worker is mid-launch)."""
    runtime, task, _ = _make_layout(tmp_path)
    state_dir = _make_state_dir_with_converter(tmp_path)
    runner = _RecordingRunner()
    expected_script = str(state_dir / "commands" / "common_transcript.sh")
    runner.respond((expected_script, "--single-pass"), _StubResult(returncode=2))

    rc = create_worker_mod.launch(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        workspace="ws-1",
        state_dir=state_dir,
        runner=runner,
    )

    assert rc == 0
    # The subsequent message send must still run.
    assert [c.argv for c in runner.calls][-1] == [
        "mngr",
        "message",
        "demo-worker",
        "--message-file",
        str(task),
    ]
    err = capsys.readouterr().err
    assert "common_transcript.sh" in err
    assert "exited 2" in err


def test_main_picks_up_state_dir_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() reads MNGR_AGENT_STATE_DIR and threads it into launch."""
    runtime, task, _ = _make_layout(tmp_path)
    state_dir = _make_state_dir_with_converter(tmp_path)
    runner = _RecordingRunner()
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(state_dir))

    rc = create_worker_mod.main(_launch_argv(runtime, task), runner=runner)

    assert rc == 0
    expected_script = str(state_dir / "commands" / "common_transcript.sh")
    flush_calls = [
        c.argv for c in runner.calls if c.argv == [expected_script, "--single-pass"]
    ]
    assert len(flush_calls) == 1


# --- await subcommand -----------------------------------------------------


class _FakeClock:
    """Monotonic clock that advances by a fixed step on every read.

    Lets ``await_report`` reach its deadline deterministically without real
    sleeping: each ``clock()`` read inside the poll loop moves time forward.
    """

    def __init__(self, step: float) -> None:
        self._now = 0.0
        self._step = step

    def __call__(self) -> float:
        now = self._now
        self._now += self._step
        return now


def _no_sleep(_seconds: float) -> None:
    return None


def _write_await_task(task_file: Path, report_path: Path) -> None:
    """Write a task file whose frontmatter points await at ``report_path``."""
    task_file.write_text(
        f"---\nlead_agent: lead\nfinish_report_path: {report_path}\n---\n\nbody\n"
    )


def test_await_returns_report_immediately_when_present(tmp_path: Path) -> None:
    """A report already on disk is printed at once, before any sleep."""
    report = tmp_path / "runtime" / "launch-task" / "demo" / "reports" / "report.md"
    report.parent.mkdir(parents=True)
    report.write_text("---\ntype: status\nname: done\n---\n\nall good\n")
    out = io.StringIO()

    def _boom(_seconds: float) -> None:
        raise AssertionError("await must not sleep when the report already exists")

    rc = create_worker_mod.await_report(
        report_path=report,
        timeout_seconds=1800,
        poll_interval_seconds=5,
        sleeper=_boom,
        clock=lambda: 0.0,
        out=out,
    )

    assert rc == 0
    assert "name: done" in out.getvalue()
    assert "all good" in out.getvalue()


def test_await_polls_until_report_appears(tmp_path: Path) -> None:
    """await loops, sleeping, until the report shows up, then prints it."""
    report = tmp_path / "runtime" / "launch-task" / "demo" / "reports" / "report.md"
    report.parent.mkdir(parents=True)
    out = io.StringIO()

    sleeps: list[float] = []

    def _sleeper_that_creates_report(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 3:
            report.write_text("---\ntype: gate\nname: question\n---\n\nwhich one?\n")

    rc = create_worker_mod.await_report(
        report_path=report,
        timeout_seconds=1800,
        poll_interval_seconds=5,
        sleeper=_sleeper_that_creates_report,
        clock=lambda: 0.0,
        out=out,
    )

    assert rc == 0
    assert sleeps == [5, 5, 5]
    assert "name: question" in out.getvalue()


def test_await_times_out_when_report_never_appears(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """When the deadline passes with no report, await returns the timeout code."""
    report = tmp_path / "runtime" / "launch-task" / "demo" / "reports" / "report.md"
    report.parent.mkdir(parents=True)
    out = io.StringIO()

    rc = create_worker_mod.await_report(
        report_path=report,
        timeout_seconds=30,
        poll_interval_seconds=5,
        sleeper=_no_sleep,
        clock=_FakeClock(step=20),
        out=out,
    )

    assert rc == create_worker_mod._AWAIT_TIMEOUT_RC
    assert out.getvalue() == ""
    assert "timed out" in capsys.readouterr().err


def test_read_finish_report_path_returns_field(tmp_path: Path) -> None:
    """_read_finish_report_path pulls the path out of the task frontmatter."""
    task = tmp_path / "task.md"
    _write_await_task(task, Path("runtime/crystallize/demo/reports/report.md"))

    result = create_worker_mod._read_finish_report_path(task)

    assert result == Path("runtime/crystallize/demo/reports/report.md")


def test_read_finish_report_path_missing_raises(tmp_path: Path) -> None:
    """A task file without finish_report_path is a hard error for await."""
    task = tmp_path / "task.md"
    task.write_text("---\nlead_agent: lead\n---\n\nbody\n")

    with pytest.raises(ValueError, match="finish_report_path"):
        create_worker_mod._read_finish_report_path(task)


def _await_argv(task_file: Path, extra: Sequence[str] = ()) -> list[str]:
    return ["await", "--task-file", str(task_file), *extra]


def test_main_await_prints_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """await parses the task file, finds finish_report_path, and prints the report.

    The report exists up front, so main()'s real ``time.sleep`` is never
    reached and the loop returns immediately.
    """
    report = tmp_path / "runtime" / "launch-task" / "demo" / "reports" / "report.md"
    report.parent.mkdir(parents=True)
    report.write_text("hello from worker\n")
    task = tmp_path / "task.md"
    _write_await_task(task, report)

    rc = create_worker_mod.main(_await_argv(task))

    assert rc == 0
    assert capsys.readouterr().out == "hello from worker\n"


def test_main_await_missing_finish_report_path_raises(tmp_path: Path) -> None:
    """await raises (full traceback) when the required field is absent, rather
    than swallowing it into a terse exit-2 message."""
    task = tmp_path / "task.md"
    task.write_text("---\nlead_agent: lead\n---\n\nbody\n")

    with pytest.raises(ValueError, match="finish_report_path"):
        create_worker_mod.main(_await_argv(task))


@pytest.mark.parametrize(
    "text,expected",
    [
        ("30m", 1800.0),
        ("90s", 90.0),
        ("1h", 3600.0),
        ("45", 45.0),
        ("2.5m", 150.0),
    ],
)
def test_parse_duration_accepts_suffixes(text: str, expected: float) -> None:
    assert create_worker_mod._parse_duration(text) == expected


@pytest.mark.parametrize("bad", ["", "abc", "-5m", "0s", "m"])
def test_parse_duration_rejects_invalid(bad: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        create_worker_mod._parse_duration(bad)


# --- parse_report ---------------------------------------------------------


def test_parse_report_extracts_frontmatter_and_body() -> None:
    result = create_worker_mod.parse_report(
        "---\ntype: status\nname: done\n---\n\nCommitted on branch `mngr/x`.\n"
    )
    assert result.report_type == "status"
    assert result.name == "done"
    assert result.body == "Committed on branch `mngr/x`."


def test_parse_report_without_frontmatter_is_none_typed() -> None:
    """An unparseable report yields None type/name and the whole text as body."""
    result = create_worker_mod.parse_report("just a body, no frontmatter\n")
    assert result.report_type is None
    assert result.name is None
    assert "just a body" in result.body


# --- destroy --------------------------------------------------------------


def test_destroy_invokes_mngr_destroy_force() -> None:
    runner = _RecordingRunner()
    create_worker_mod.destroy("demo-worker", runner=runner)
    assert [c.argv for c in runner.calls] == [
        ["mngr", "destroy", "demo-worker", "--force"]
    ]


# --- run_sync -------------------------------------------------------------


def _make_run_layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    """runtime_dir + task_file (with finish_report_path) + the report path."""
    runtime = tmp_path / "runtime" / "launch-task" / "demo"
    (runtime / "reports").mkdir(parents=True)
    report = runtime / "reports" / "report.md"
    task = runtime / "task.md"
    task.write_text(
        f"---\nlead_agent: lead\nfinish_report_path: {report}\n---\n\nbody\n"
    )
    return runtime, task, report


def _boom_sleeper(_seconds: float) -> None:
    raise AssertionError("run_sync must not sleep when the report already exists")


def test_run_sync_collects_report_and_destroys(tmp_path: Path) -> None:
    runtime, task, report = _make_run_layout(tmp_path)
    report.write_text("---\ntype: status\nname: done\n---\n\nReady to merge.\n")
    runner = _RecordingRunner()
    out = io.StringIO()

    rc = create_worker_mod.run_sync(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        workspace="ws-1",
        timeout_seconds=1800,
        poll_interval_seconds=5,
        destroy_on_finish=True,
        runner=runner,
        sleeper=_boom_sleeper,
        clock=lambda: 0.0,
        out=out,
    )

    assert rc == 0
    payload = json.loads(out.getvalue())
    assert payload == {
        "timed_out": False,
        "type": "status",
        "name": "done",
        "body": "Ready to merge.",
        "branch": "mngr/demo-worker",
        "raw_report": report.read_text(),
    }
    # The lifecycle ran create -> rsync -> message, then destroy after the report.
    assert ["mngr", "destroy", "demo-worker", "--force"] in [
        c.argv for c in runner.calls
    ]


def test_run_sync_writes_result_json_file(tmp_path: Path) -> None:
    """``result_path`` receives the same JSON payload as stdout, so a programmatic
    caller reads an unambiguous file instead of scraping the last stdout line."""
    runtime, task, report = _make_run_layout(tmp_path)
    report.write_text("---\ntype: status\nname: done\n---\n\nReady to merge.\n")
    runner = _RecordingRunner()
    out = io.StringIO()
    result_path = tmp_path / "out" / "result.json"

    rc = create_worker_mod.run_sync(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        workspace="ws-1",
        timeout_seconds=1800,
        poll_interval_seconds=5,
        destroy_on_finish=True,
        runner=runner,
        sleeper=_boom_sleeper,
        clock=lambda: 0.0,
        out=out,
        result_path=result_path,
    )

    assert rc == 0
    # The file holds exactly the stdout payload (the machine-readable contract).
    assert json.loads(result_path.read_text()) == json.loads(out.getvalue())
    assert json.loads(result_path.read_text())["name"] == "done"


def test_run_sync_timeout_does_not_destroy(tmp_path: Path) -> None:
    """A timeout returns the timeout code, emits timed_out JSON, and leaves the
    worker alive for liveness diagnosis."""
    runtime, task, _ = _make_run_layout(tmp_path)  # report never written
    runner = _RecordingRunner()
    out = io.StringIO()

    rc = create_worker_mod.run_sync(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        workspace="ws-1",
        timeout_seconds=30,
        poll_interval_seconds=5,
        destroy_on_finish=True,
        runner=runner,
        sleeper=_no_sleep,
        clock=_FakeClock(step=20),
        out=out,
    )

    assert rc == create_worker_mod._AWAIT_TIMEOUT_RC
    assert json.loads(out.getvalue())["timed_out"] is True
    assert not any(c.argv[:2] == ["mngr", "destroy"] for c in runner.calls)


def test_run_sync_keep_agent_skips_destroy(tmp_path: Path) -> None:
    runtime, task, report = _make_run_layout(tmp_path)
    report.write_text("---\ntype: status\nname: done\n---\n\nReady to merge.\n")
    runner = _RecordingRunner()
    out = io.StringIO()

    rc = create_worker_mod.run_sync(
        name="demo-worker",
        template="worker",
        runtime_dir=runtime,
        task_file=task,
        workspace="ws-1",
        timeout_seconds=1800,
        poll_interval_seconds=5,
        destroy_on_finish=False,
        runner=runner,
        sleeper=_boom_sleeper,
        clock=lambda: 0.0,
        out=out,
    )

    assert rc == 0
    assert not any(c.argv[:2] == ["mngr", "destroy"] for c in runner.calls)


def test_main_destroy_dispatch() -> None:
    runner = _RecordingRunner()
    rc = create_worker_mod.main(["destroy", "--name", "demo-worker"], runner=runner)
    assert rc == 0
    assert [c.argv for c in runner.calls] == [
        ["mngr", "destroy", "demo-worker", "--force"]
    ]


def test_main_run_missing_finish_report_path_raises(tmp_path: Path) -> None:
    """run validates finish_report_path up front and raises (full traceback)
    rather than launching without it -- a missing required field is an
    authoring bug, not a recoverable exit-2 condition."""
    runtime = tmp_path / "runtime" / "launch-task" / "demo"
    runtime.mkdir(parents=True)
    task = runtime / "task.md"
    task.write_text("---\nlead_agent: lead\n---\n\nbody\n")
    runner = _RecordingRunner()

    with pytest.raises(ValueError, match="finish_report_path"):
        create_worker_mod.main(
            [
                "run",
                "--name",
                "x",
                "--template",
                "worker",
                "--runtime-dir",
                str(runtime),
                "--task-file",
                str(task),
            ],
            runner=runner,
        )

    assert runner.calls == []
