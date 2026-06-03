#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""Worker-creation driver for the launch-task family of skills.

Two subcommands cover the two halves of the lead-side lifecycle:

``launch``
    Runs the worker-creation lifecycle synchronously (``mngr create`` + the
    runtime-dir sync + the task message) and returns. Callers run this in the
    *foreground* so a failed launch surfaces immediately rather than as a
    delayed background notification.

``await``
    Reads the ``finish_report_path`` field from the task file's frontmatter and
    blocks until that file appears, prints its contents to stdout, and returns
    0. On timeout it returns non-zero so the caller drops into the liveness
    diagnosis described in ``.agents/shared/references/lead-proxy.md``. Callers
    run this in the *background* and re-invoke it once per gate cycle; it is
    deliberately dumb -- it only waits and cats. Parsing the report, deciding
    answer-vs-escalate, consuming the report into ``consumed/``, and merging are
    all lead judgment and stay in ``lead-proxy.md``.

Both subcommands take the same ``--task-file``: ``launch`` sends it to the
worker, and ``await`` reads its ``finish_report_path`` to learn what to wait
for. Putting the wait target in frontmatter (rather than deriving a fixed path)
keeps the contract data-driven, so future flows can point ``await`` at a
different report without a code change.

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
and syncs the directory alongside the runtime dir -- no extra CLI flag.

Launch lifecycle commands:

    mngr create <NAME> -t <TEMPLATE> --label workspace=<MINDS_WORKSPACE_NAME>
    mngr rsync  <RUNTIME_DIR>/   <NAME>:<RUNTIME_DIR>/   --uncommitted-changes=merge
    mngr rsync  <ARTIFACTS_DIR>/ <NAME>:<ARTIFACTS_DIR>/ --uncommitted-changes=merge
                (when frontmatter declares it)
    mngr message <NAME> --message-file <TASK_FILE>

``mngr rsync`` takes ``SOURCE DESTINATION`` (the local source dir first, then
the ``<NAME>:<PATH>`` agent endpoint). The trailing slash on both ends makes
rsync copy directory *contents* into the destination; mngr passes the paths
through verbatim, so the slash is load-bearing. The
``--uncommitted-changes=merge`` flag is required (see
``.agents/shared/references/lead-proxy.md``).

Why ``mngr message`` *after* the syncs (instead of using ``mngr create
--message-file``): if the worker reads its first message before the runtime
dir sync lands in its worktree, the task file's ``finish_report_path`` will
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


def _read_frontmatter_field(task_file: Path, key: str) -> str | None:
    """Return the string value of frontmatter ``key``, or ``None`` if absent.

    Returns ``None`` when the file has no frontmatter block (no leading ``---``)
    or the key is missing -- full schema validation is the worker's job
    (``parse_task_frontmatter.py``); here we pull out one key at a time.

    Raises ``ValueError`` when the frontmatter is genuinely malformed -- a
    present block whose body is invalid YAML, or a key present but not a
    non-empty string. A broken frontmatter block is an authoring bug in the
    task file, so we surface it (with the original parse error chained) rather
    than silently degrading it to "field absent" and launching the worker with
    the wrong inputs.
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
    except yaml.YAMLError as exc:
        raise ValueError(
            f"{task_file}: frontmatter block is present but contains invalid YAML"
        ) from exc
    if not isinstance(frontmatter, dict):
        return None
    value = frontmatter.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"frontmatter.{key} must be a non-empty string")
    return value


def _read_source_artifacts_dir(task_file: Path) -> Path | None:
    """Return the optional ``source_artifacts_dir`` declared in the task
    frontmatter, or ``None`` when absent.

    The caller sets this key when the worker needs gitignored auxiliary state
    that lives outside the runtime dir; launch syncs that directory alongside
    the runtime dir.
    """
    value = _read_frontmatter_field(task_file, "source_artifacts_dir")
    return Path(value) if value is not None else None


def _read_finish_report_path(task_file: Path) -> Path:
    """Return the ``finish_report_path`` the worker writes its report to.

    This is the file ``await`` polls for. Required: raises ``ValueError`` if
    the key is absent (or present but not a non-empty string).
    """
    value = _read_frontmatter_field(task_file, "finish_report_path")
    if value is None:
        raise ValueError("frontmatter is missing required field `finish_report_path`")
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
    the runtime sync and the message send). On non-zero exit we log a
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


def rsync_dir(name: str, source_dir: Path, runner: Runner) -> None:
    """Rsync ``source_dir`` into worker ``name``'s worktree at the same path.

    ``mngr rsync`` takes ``SOURCE DESTINATION``: the local ``source_dir`` first,
    then the ``<name>:<path>`` agent endpoint. The directory form (trailing
    slash on both sides) makes rsync copy the directory *contents* into the
    destination rather than nesting it, and ``--uncommitted-changes=merge``
    keeps the worker's post-create uncommitted state from refusing the sync --
    see lead-proxy.md § "mngr rsync rationale" for why both are required.
    """
    normalized = _normalize_dir(str(source_dir))
    runner.run(
        [
            "mngr",
            "rsync",
            normalized,
            f"{name}:{normalized}",
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

    Pre-flight checks run first so a typo doesn't half-create a worker:
    ``runtime_dir`` and ``task_file`` existence (and any declared
    ``source_artifacts_dir``'s existence) return exit code 2 with a clean
    message, since those are caller-supplied paths. Malformed task-file
    frontmatter instead raises ``ValueError`` (full traceback) -- that's a bug
    in how the task file was composed, not a bad CLI argument.

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
    # A malformed ``source_artifacts_dir`` (or invalid frontmatter YAML) is an
    # authoring bug in the task file -- let it raise so the caller gets the full
    # traceback rather than a terse one-line message. The CLI path checks above
    # stay as clean exit-2 validations (those are caller-supplied arguments, not
    # file content).
    artifacts_dir = _read_source_artifacts_dir(task_file)
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

    rsync_dir(name, runtime_dir, runner)
    if artifacts_dir is not None:
        rsync_dir(name, artifacts_dir, runner)

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

    print(f"create_worker: worker {name} launched and runtime synced")
    return 0


def await_report(
    report_path: Path,
    timeout_seconds: float,
    poll_interval_seconds: float,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    out: TextIO | None = None,
) -> int:
    """Block until ``report_path`` exists, then print its contents.

    Returns 0 after writing the report contents to ``out`` (default stdout);
    returns ``_AWAIT_TIMEOUT_RC`` if the deadline passes first, leaving a note
    on stderr so the caller diagnoses worker liveness per lead-proxy.md rather
    than treating the timeout as a terminal failure.

    ``sleeper``/``clock`` are injected so tests can drive the poll loop without
    real time. The file is checked before the first sleep, so a report already
    present returns immediately.
    """
    stream: TextIO = sys.stdout if out is None else out
    deadline = clock() + timeout_seconds
    while True:
        if report_path.is_file():
            stream.write(report_path.read_text(encoding="utf-8"))
            return 0
        if clock() >= deadline:
            print(
                f"create_worker: timed out after {timeout_seconds:g}s waiting for "
                f"{report_path}; the worker may still be alive -- diagnose liveness "
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
    # A missing/malformed ``finish_report_path`` is an authoring bug in the task
    # file; let the ValueError raise for a full traceback rather than swallowing
    # it into a terse exit-2 message (matches ``launch``'s handling above).
    report_path = _read_finish_report_path(args.task_file)
    return await_report(
        report_path=report_path,
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
        help="Existing runtime directory synced verbatim into the worker's worktree.",
    )
    launch_parser.add_argument(
        "--task-file",
        required=True,
        type=Path,
        help="Markdown task file (must already exist; typically inside --runtime-dir).",
    )

    await_parser = subparsers.add_parser(
        "await",
        help="Block until the worker's report file appears, then print it. "
        "Run in the background; re-invoke once per gate cycle.",
    )
    await_parser.add_argument(
        "--task-file",
        required=True,
        type=Path,
        help="Same task file as launch; its frontmatter `finish_report_path` "
        "names the file to wait for.",
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
