#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""Worker-creation driver for the launch-task family of skills.

Two subcommands cover the two halves of the lead-side lifecycle:

``launch``
    Runs the worker-creation lifecycle synchronously (``mngr create`` + the
    runtime-dir push + the task message) and returns. Callers run this in the
    *foreground* so a failed launch surfaces immediately rather than as a
    delayed background notification.

``await``
    Blocks until the worker's ``report.md`` appears under the runtime dir's
    ``reports/`` subdirectory, prints its contents to stdout, and returns 0.
    On timeout it returns non-zero so the caller drops into the liveness
    diagnosis described in ``.agents/shared/references/lead-proxy.md``. Callers
    run this in the *background* and re-invoke it once per gate cycle; it is
    deliberately dumb -- it only waits and cats. Parsing the report, deciding
    answer-vs-escalate, consuming ``report.md`` into ``consumed/``, and merging
    are all lead judgment and stay in ``lead-proxy.md``.

Both subcommands key off the same ``--runtime-dir`` (``runtime/<feature>/<slug>/``):
``launch`` pushes it into the worker's worktree, and ``await`` derives
``<runtime-dir>/reports/report.md`` from it. All launch-task-family flows use
this identical layout, so there is no per-flow path to interpolate.

The caller is responsible for writing the task file (with whatever YAML
frontmatter the worker template requires) and for placing it -- and any
gitignored auxiliary state -- under ``runtime/<feature>/<slug>/`` before
calling ``launch``. This script orchestrates the lifecycle commands; it does
not compose task content.

Ticket bookkeeping (``tk create`` / ``tk start`` / ``tk close``) is the
caller's responsibility -- it lives in the calling skill's prose so each
flow can shape the ticket title, type, and acceptance criteria itself.

When the worker needs gitignored auxiliary state (scripts, sample data)
that lives outside the runtime dir, the caller declares it in the task
frontmatter with a ``source_artifacts_dir`` key; launch reads that key
and pushes the directory alongside the runtime dir -- no extra CLI flag.

Launch lifecycle commands:

    mngr create <NAME> -t <TEMPLATE> --label workspace=<MINDS_WORKSPACE_NAME>
    mngr push   <NAME>:<RUNTIME_DIR>/   --source <RUNTIME_DIR>/
                --uncommitted-changes=merge
    mngr push   <NAME>:<ARTIFACTS_DIR>/ --source <ARTIFACTS_DIR>/
                --uncommitted-changes=merge   (when frontmatter declares it)
    mngr message <NAME> --message-file <TASK_FILE>

The trailing-slash rewriting and ``--uncommitted-changes=merge`` flag are
required by ``mngr push`` (see ``.agents/shared/references/lead-proxy.md``).

Why ``mngr message`` *after* the pushes (instead of using ``mngr create
--message-file``): if the worker reads its first message before the runtime
dir push lands in its worktree, the task file's ``lead_report_dir`` will
resolve to nothing. Sending the task as a follow-up message guarantees the
worker sees the runtime dir first.

Common-transcript flush: right before sending the task message we invoke the
lead's own ``common_transcript.sh --single-pass`` converter (when present).
This guarantees the worker's first ``mngr transcript <lead>`` read includes
every turn up through the handoff -- the converter normally polls on a 5s
interval, which races with worker startup. It only freshens through the
handoff moment; later lead turns won't appear until the poller catches up,
which is fine for the anchored-lookup pattern (workers locate quotes the
lead already pasted into the task body).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Sequence, TextIO

import yaml

_COMMON_TRANSCRIPT_REL = Path("commands/common_transcript.sh")
_REPORT_REL = Path("reports/report.md")

_DEFAULT_TIMEOUT = "30m"
_DEFAULT_POLL_INTERVAL = "5s"

# Distinct exit code for an await that timed out without the report appearing,
# matching coreutils ``timeout``'s convention so the prose's mental model
# carries over.
_AWAIT_TIMEOUT_RC = 124


def _normalize_dir(value: str) -> str:
    """Return ``value`` with exactly one trailing slash."""
    return value.rstrip("/") + "/"


def _parse_duration(value: str) -> float:
    """Parse a duration like ``30m``, ``90s``, ``1h``, or a bare integer (seconds).

    Mirrors the ``timeout 30m`` idiom the lead-proxy prose used before this was
    a script, so the same values keep working.
    """
    text = value.strip().lower()
    if not text:
        raise argparse.ArgumentTypeError("duration must not be empty")
    units = {"s": 1, "m": 60, "h": 3600}
    unit = units.get(text[-1])
    number = text[:-1] if unit is not None else text
    multiplier = unit if unit is not None else 1
    try:
        magnitude = float(number)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid duration {value!r}; use e.g. '30m', '90s', '1h', or seconds"
        )
    if magnitude <= 0:
        raise argparse.ArgumentTypeError(f"duration must be positive: {value!r}")
    return magnitude * multiplier


def _read_source_artifacts_dir(task_file: Path) -> Path | None:
    """Return the directory declared by the task frontmatter's
    ``source_artifacts_dir`` key, or ``None`` when the key is absent.

    The caller sets this key when the worker needs gitignored auxiliary
    state that lives outside the runtime dir; launch pushes that
    directory alongside the runtime dir. Validating the rest of the
    frontmatter schema is the worker's job (``parse_task_frontmatter.py``);
    here we only pull out this one key, and only raise if it is present
    but not a non-empty string.
    """
    lines = task_file.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end_idx = lines.index("---", 1)
    except ValueError:
        return None
    try:
        frontmatter = yaml.safe_load("\n".join(lines[1:end_idx]))
    except yaml.YAMLError:
        return None
    if not isinstance(frontmatter, dict):
        return None
    value = frontmatter.get("source_artifacts_dir")
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("frontmatter.source_artifacts_dir must be a non-empty string")
    return Path(value)


class Runner:
    """Indirection over ``subprocess.run`` so tests can intercept commands.

    The default implementation calls ``subprocess.run`` directly. Tests
    inject a recording stub instead.
    """

    def run(self, argv: Sequence[str], **kwargs):
        return subprocess.run(list(argv), **kwargs)


def _flush_common_transcript(state_dir: Path | None, runner: Runner) -> None:
    """Run the lead's common-transcript converter once, synchronously.

    No-op when ``state_dir`` is unset (tests, non-mngr environments) or the
    converter script isn't installed at the standard path (non-claude agents
    don't have it). See module docstring for why this runs before the message
    send.

    Best-effort by design: this is a freshness optimization that merely
    races the converter's 5s poller, so a converter failure must not
    abort launch (which would orphan a half-launched worker between
    the runtime push and the message send). On non-zero exit we log a
    warning to stderr and let launch continue; the worker will see
    whatever the periodic poller has already produced.
    """
    if state_dir is None:
        return
    script = state_dir / _COMMON_TRANSCRIPT_REL
    if not script.is_file():
        return
    result = runner.run([str(script), "--single-pass"], check=False)
    returncode = getattr(result, "returncode", 0)
    if returncode != 0:
        print(
            f"create_worker: warning: common_transcript.sh --single-pass exited "
            f"{returncode}; worker will read whatever the periodic poller "
            f"has already produced",
            file=sys.stderr,
        )


def push(name: str, source_dir: Path, runner: Runner) -> None:
    """Push ``source_dir`` into worker ``name``'s worktree at the same path.

    Uses the directory form (trailing slash on both sides) and
    ``--uncommitted-changes=merge`` -- see lead-proxy.md § "mngr push
    rationale" for why both are required.
    """
    normalized = _normalize_dir(str(source_dir))
    runner.run(
        [
            "mngr",
            "push",
            f"{name}:{normalized}",
            "--source",
            normalized,
            "--uncommitted-changes=merge",
        ],
        check=True,
    )


def launch(
    name: str,
    template: str,
    runtime_dir: Path,
    task_file: Path,
    workspace: str,
    state_dir: Path | None = None,
    runner: Runner | None = None,
) -> int:
    """Run the worker-creation lifecycle. Returns the process exit code.

    Pre-flight checks (existence of ``runtime_dir``, ``task_file``, and any
    ``source_artifacts_dir`` declared in the task frontmatter) run first so
    a typo doesn't half-create a worker.

    ``state_dir`` is the lead's ``MNGR_AGENT_STATE_DIR``; when set, the
    converter at ``<state_dir>/commands/common_transcript.sh`` is flushed
    before the task message lands so the worker's first transcript read
    sees fresh events.
    """
    runner = runner or Runner()

    if not runtime_dir.is_dir():
        print(
            f"create_worker: --runtime-dir is not a directory: {runtime_dir}",
            file=sys.stderr,
        )
        return 2
    if not task_file.is_file():
        print(f"create_worker: --task-file not found: {task_file}", file=sys.stderr)
        return 2
    try:
        artifacts_dir = _read_source_artifacts_dir(task_file)
    except ValueError as exc:
        print(f"create_worker: {exc}", file=sys.stderr)
        return 2
    if artifacts_dir is not None and not artifacts_dir.is_dir():
        print(
            f"create_worker: source_artifacts_dir is not a directory: {artifacts_dir}",
            file=sys.stderr,
        )
        return 2

    runner.run(
        [
            "mngr",
            "create",
            name,
            "-t",
            template,
            "--label",
            f"workspace={workspace}",
        ],
        check=True,
    )

    push(name, runtime_dir, runner)
    if artifacts_dir is not None:
        push(name, artifacts_dir, runner)

    _flush_common_transcript(state_dir, runner)

    runner.run(
        [
            "mngr",
            "message",
            name,
            "--message-file",
            str(task_file),
        ],
        check=True,
    )

    print(f"create_worker: worker {name} launched and runtime pushed")
    return 0


def await_report(
    runtime_dir: Path,
    timeout_seconds: float,
    poll_interval_seconds: float,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    out: TextIO | None = None,
) -> int:
    """Block until ``<runtime_dir>/reports/report.md`` exists, then print it.

    Returns 0 after writing the report contents to ``out`` (default stdout);
    returns ``_AWAIT_TIMEOUT_RC`` if the deadline passes first, leaving a note
    on stderr so the caller diagnoses worker liveness per lead-proxy.md rather
    than treating the timeout as a terminal failure.

    ``sleeper``/``clock`` are injected so tests can drive the poll loop without
    real time. The file is checked before the first sleep, so a report already
    present returns immediately.
    """
    stream: TextIO = sys.stdout if out is None else out
    report = runtime_dir / _REPORT_REL
    deadline = clock() + timeout_seconds
    while True:
        if report.is_file():
            stream.write(report.read_text(encoding="utf-8"))
            return 0
        if clock() >= deadline:
            print(
                f"create_worker: timed out after {timeout_seconds:g}s waiting for "
                f"{report}; the worker may still be alive -- diagnose liveness "
                f"per lead-proxy.md before invoking the failure flow",
                file=sys.stderr,
            )
            return _AWAIT_TIMEOUT_RC
        sleeper(poll_interval_seconds)


def _run_launch(args: argparse.Namespace, runner: Runner | None) -> int:
    workspace = os.environ.get("MINDS_WORKSPACE_NAME", "default")
    state_dir_env = os.environ.get("MNGR_AGENT_STATE_DIR")
    state_dir = Path(state_dir_env) if state_dir_env else None
    return launch(
        name=args.name,
        template=args.template,
        runtime_dir=args.runtime_dir,
        task_file=args.task_file,
        workspace=workspace,
        state_dir=state_dir,
        runner=runner,
    )


def _run_await(args: argparse.Namespace) -> int:
    return await_report(
        runtime_dir=args.runtime_dir,
        timeout_seconds=args.timeout,
        poll_interval_seconds=args.poll_interval,
    )


def main(argv: Sequence[str] | None = None, runner: Runner | None = None) -> int:
    """CLI entry point. Tests inject ``runner`` to capture the launch argv lifecycle."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    launch_parser = subparsers.add_parser(
        "launch", help="Create the worker and hand it the task (synchronous)."
    )
    launch_parser.add_argument(
        "--name", required=True, help="Worker name; becomes the mngr/<name> branch."
    )
    launch_parser.add_argument(
        "--template",
        required=True,
        help="mngr create template (e.g. 'worker', 'crystallize-worker').",
    )
    launch_parser.add_argument(
        "--runtime-dir",
        required=True,
        type=Path,
        help="Existing runtime directory pushed verbatim into the worker's worktree.",
    )
    launch_parser.add_argument(
        "--task-file",
        required=True,
        type=Path,
        help="Markdown task file (must already exist; typically inside --runtime-dir).",
    )

    await_parser = subparsers.add_parser(
        "await",
        help="Block until the worker's report.md appears, then print it. "
        "Run in the background; re-invoke once per gate cycle.",
    )
    await_parser.add_argument(
        "--runtime-dir",
        required=True,
        type=Path,
        help="Same runtime dir as launch; report.md is read from its reports/ subdir.",
    )
    await_parser.add_argument(
        "--timeout",
        default=_DEFAULT_TIMEOUT,
        type=_parse_duration,
        help=f"Max wait before giving up (default {_DEFAULT_TIMEOUT}). "
        "Accepts e.g. '30m', '90s', '1h', or bare seconds.",
    )
    await_parser.add_argument(
        "--poll-interval",
        default=_DEFAULT_POLL_INTERVAL,
        type=_parse_duration,
        help=f"How often to check for the report (default {_DEFAULT_POLL_INTERVAL}).",
    )

    args = parser.parse_args(argv)

    if args.command == "launch":
        return _run_launch(args, runner)
    return _run_await(args)


if __name__ == "__main__":
    sys.exit(main())
