#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""Worker-creation driver for the launch-task family of skills.

Four subcommands cover the lead-side lifecycle:

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

``launch-sync``
    The blocking one-call path for non-interactive callers (services): launch,
    wait for the report in the *foreground*, emit a structured-result JSON
    object (``timed_out`` plus the report ``type``/``name``/``body`` and the
    worker ``branch``), and destroy the worker. ``--result-json`` also writes
    that JSON to a caller-named file as the machine-readable contract.
    ``--keep-agent`` skips the destroy; a timeout never destroys (the report may
    still be coming).

``destroy``
    Destroys the worker agent (``mngr destroy <name> --force``). The git branch
    ``mngr/<name>`` survives in the shared object store, so the work can still
    be merged or inspected.

The ``launch`` / ``await`` / ``launch-sync`` subcommands take the same
``--task-file``: ``launch`` sends it to the worker, and ``await`` /
``launch-sync`` read its ``finish_report_path`` to learn what to wait for.
Putting the wait target in frontmatter (rather than deriving a fixed path) keeps
the contract data-driven, so future flows can point the wait at a different
report without a code change.

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
    mngr rsync  ./<RUNTIME_DIR>/   <NAME>:<RUNTIME_DIR>/   --uncommitted-changes=merge
    mngr rsync  ./<ARTIFACTS_DIR>/ <NAME>:<ARTIFACTS_DIR>/ --uncommitted-changes=merge
                (when frontmatter declares it)
    mngr message <NAME> --message-file <TASK_FILE>

``mngr rsync`` takes ``SOURCE DESTINATION`` (the local source dir first, then
the ``<NAME>:<PATH>`` agent endpoint). The trailing slash on both ends makes
rsync copy directory *contents* into the destination. The local source is
``./``-prefixed so mngr reads it as a path rather than an agent name, while the
agent destination stays repo-relative so mngr resolves it against the worker's
workdir. The ``--uncommitted-changes=merge`` flag is required (see
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
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Mapping, NamedTuple, Sequence, TextIO

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


def _split_frontmatter(text: str) -> tuple[dict[str, object] | None, str]:
    """Split leading YAML frontmatter from the body.

    Returns ``(frontmatter, body)``. ``frontmatter`` is ``None`` when there is no
    leading ``---`` fence, the closing fence is missing, or the parsed YAML is not
    a mapping; otherwise it is the parsed mapping. ``body`` is the text below the
    closing fence (``""`` when there is no fence). Raises ``yaml.YAMLError`` when a
    fenced block contains invalid YAML -- callers decide whether to surface that
    (an authoring bug in a deterministic input) or swallow it (tolerant parsing of
    agent-authored runtime output). The shared scan/parse for both callers.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, ""
    try:
        end_idx = lines.index("---", 1)
    except ValueError:
        return None, ""
    frontmatter = yaml.safe_load("\n".join(lines[1:end_idx]))
    body = "\n".join(lines[end_idx + 1 :]).strip("\n")
    if not isinstance(frontmatter, dict):
        return None, body
    return frontmatter, body


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
    try:
        frontmatter, _body = _split_frontmatter(task_file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(
            f"{task_file}: frontmatter block is present but contains invalid YAML"
        ) from exc
    if frontmatter is None:
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
    keeps the worker's post-create uncommitted state from refusing the sync.

    Two path details are load-bearing (see lead-proxy.md § "mngr rsync
    rationale"):

    - The local SOURCE is ``./``-prefixed when ``source_dir`` is relative.
      ``mngr rsync`` only treats a path starting with ``/``, ``./``, ``../`` or
      ``~/`` as local; a bare ``runtime/foo/`` would be misparsed as an *agent
      name* and the command would fail.
    - The agent DESTINATION keeps the bare repo-relative path. mngr resolves a
      relative agent ``:PATH`` against the worker's workdir (its worktree root),
      so the dir lands at the same relative location inside the worker rather
      than wherever the lead happens to be running.
    """
    rel = _normalize_dir(str(source_dir))
    local_source = rel if rel.startswith(("/", "./", "../", "~/")) else f"./{rel}"
    runner.run(
        [
            "mngr",
            "rsync",
            local_source,
            f"{name}:{rel}",
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


class ReportResult(NamedTuple):
    """Structured view of a worker's terminal/gate report.

    ``report_type`` and ``name`` are the frontmatter ``type``/``name`` fields, or
    ``None`` when the report has no parseable frontmatter (caller treats that as a
    failure). ``body`` is the prose below the frontmatter; ``raw`` is the verbatim
    report text.
    """

    report_type: str | None
    name: str | None
    body: str
    raw: str


def parse_report(text: str) -> ReportResult:
    """Parse a worker report's YAML frontmatter (``type``/``name``) and body.

    Deliberately tolerant -- unlike ``_read_frontmatter_field``, which strictly
    raises on a malformed *task file* (a lead-authored, deterministic input
    where bad YAML is an authoring bug). A report is *agent-authored runtime
    output*: an unparseable one yields ``report_type=None``/``name=None`` with
    the whole text preserved as the body, so the caller (``launch_sync``) surfaces
    the raw report as structured data for the calling process to handle, rather
    than crashing the collection path and losing the worker's output. No data is
    discarded -- ``raw`` always holds the verbatim text.
    """
    try:
        frontmatter, body = _split_frontmatter(text)
    except yaml.YAMLError:
        return ReportResult(None, None, text, text)
    if frontmatter is None:
        # No parseable frontmatter: preserve the whole text as the body so no
        # data is discarded (and the caller can still surface the raw report).
        return ReportResult(None, None, text, text)
    report_type = frontmatter.get("type")
    name = frontmatter.get("name")
    return ReportResult(
        report_type=report_type if isinstance(report_type, str) else None,
        name=name if isinstance(name, str) else None,
        body=body,
        raw=text,
    )


def destroy(name: str, runner: Runner | None = None) -> None:
    """Destroy the worker agent. The git branch ``mngr/<name>`` survives.

    ``mngr destroy`` removes the agent and its worktree; the branch persists in
    the shared object store, so a caller can still merge or inspect the work.
    """
    runner = runner or Runner()
    runner.run(["mngr", "destroy", name, "--force"], check=True)


def _emit_run_result(
    payload: Mapping[str, object], stream: TextIO, result_path: Path | None
) -> None:
    """Write the run-result JSON to stdout and, if given, to a dedicated file.

    The file is the machine contract for programmatic callers: they read the exact
    payload from a path they chose, rather than guessing which stdout line is the
    result. Stdout still carries the JSON for humans and shell callers.
    """
    line = json.dumps(payload)
    stream.write(line + "\n")
    if result_path is not None:
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(line, encoding="utf-8")


def launch_sync(
    name: str,
    template: str,
    runtime_dir: Path,
    task_file: Path,
    workspace: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
    destroy_on_finish: bool = True,
    state_dir: Path | None = None,
    runner: Runner | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    out: TextIO | None = None,
    result_path: Path | None = None,
) -> int:
    """Launch a worker, wait for its report in the *foreground*, emit JSON, destroy.

    The blocking path for non-interactive callers (services). Returns the
    launch exit code if launch fails, the await timeout code if the report never
    appears (without destroying -- the report may still be coming), or 0 once a
    report is collected. Writes a single JSON object to ``out`` (default stdout)
    describing the outcome: ``timed_out`` plus the report ``type``/``name``/``body``
    and the worker ``branch``. When ``result_path`` is set, the same JSON is also
    written there as the machine-readable contract for programmatic callers.
    """
    runner = runner or Runner()
    stream: TextIO = sys.stdout if out is None else out

    # Resolve the wait target *before* creating the worker: a missing/malformed
    # ``finish_report_path`` is an authoring bug, and reading it up front keeps it
    # from half-creating a worker (matching ``launch``'s preflight contract). The
    # field comes from the task file's frontmatter, which already exists, so this
    # is safe to read this early.
    report_path = _read_finish_report_path(task_file)

    launch_rc = launch(
        name=name,
        template=template,
        runtime_dir=runtime_dir,
        task_file=task_file,
        workspace=workspace,
        state_dir=state_dir,
        runner=runner,
    )
    if launch_rc != 0:
        return launch_rc

    buffer = io.StringIO()
    await_rc = await_report(
        report_path=report_path,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        sleeper=sleeper,
        clock=clock,
        out=buffer,
    )
    branch = f"mngr/{name}"
    if await_rc != 0:
        # Timed out: leave the worker alive for liveness diagnosis.
        _emit_run_result(
            {
                "timed_out": True,
                "type": None,
                "name": None,
                "body": "",
                "branch": branch,
                "raw_report": "",
            },
            stream,
            result_path,
        )
        return await_rc

    report = parse_report(buffer.getvalue())
    if destroy_on_finish:
        destroy(name, runner)
    _emit_run_result(
        {
            "timed_out": False,
            "type": report.report_type,
            "name": report.name,
            "body": report.body,
            "branch": branch,
            "raw_report": report.raw,
        },
        stream,
        result_path,
    )
    return 0


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


def _run_launch_sync(args: argparse.Namespace, runner: Runner | None) -> int:
    # Validate the wait target up front so a missing/malformed field fails like
    # await -- a ValueError here is an authoring bug, so let it raise with a full
    # traceback rather than swallowing it.
    _read_finish_report_path(args.task_file)
    workspace = os.environ.get("MINDS_WORKSPACE_NAME", "default")
    state_dir_env = os.environ.get("MNGR_AGENT_STATE_DIR")
    state_dir = Path(state_dir_env) if state_dir_env else None
    return launch_sync(
        name=args.name,
        template=args.template,
        runtime_dir=args.runtime_dir,
        task_file=args.task_file,
        workspace=workspace,
        timeout_seconds=args.timeout,
        poll_interval_seconds=args.poll_interval,
        destroy_on_finish=not args.keep_agent,
        state_dir=state_dir,
        runner=runner,
        result_path=args.result_json,
    )


def _run_destroy(args: argparse.Namespace, runner: Runner | None) -> int:
    destroy(args.name, runner)
    return 0


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
        help="mngr create template (e.g. 'worker', 'subskill-worker').",
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

    launch_sync_parser = subparsers.add_parser(
        "launch-sync",
        help="Blocking launch + foreground await + structured-result JSON + "
        "destroy, in one call. For non-interactive callers (services).",
    )
    launch_sync_parser.add_argument(
        "--name", required=True, help="Worker name; becomes the mngr/<name> branch."
    )
    launch_sync_parser.add_argument(
        "--template",
        required=True,
        help="mngr create template (e.g. 'worker', 'crystallize-worker').",
    )
    launch_sync_parser.add_argument(
        "--runtime-dir",
        required=True,
        type=Path,
        help="Existing runtime directory synced verbatim into the worker's worktree.",
    )
    launch_sync_parser.add_argument(
        "--task-file",
        required=True,
        type=Path,
        help="Markdown task file; its frontmatter `finish_report_path` names the "
        "report to wait for.",
    )
    launch_sync_parser.add_argument(
        "--timeout",
        default=_DEFAULT_TIMEOUT,
        type=_parse_duration,
        help=f"Max wait for the report (default {_DEFAULT_TIMEOUT}).",
    )
    launch_sync_parser.add_argument(
        "--poll-interval",
        default=_DEFAULT_POLL_INTERVAL,
        type=_parse_duration,
        help=f"How often to check for the report (default {_DEFAULT_POLL_INTERVAL}).",
    )
    launch_sync_parser.add_argument(
        "--keep-agent",
        action="store_true",
        help="Do not destroy the worker after a report is collected "
        "(default: destroy). A timeout never destroys regardless.",
    )
    launch_sync_parser.add_argument(
        "--result-json",
        type=Path,
        default=None,
        help="Also write the result JSON to this path (the machine-readable "
        "contract for programmatic callers; stdout still carries it too).",
    )

    destroy_parser = subparsers.add_parser(
        "destroy",
        help="Destroy a worker agent (mngr destroy --force). The mngr/<name> "
        "branch survives.",
    )
    destroy_parser.add_argument("--name", required=True, help="Worker name to destroy.")

    args = parser.parse_args(argv)

    if args.command == "launch":
        return _run_launch(args, runner)
    if args.command == "launch-sync":
        return _run_launch_sync(args, runner)
    if args.command == "destroy":
        return _run_destroy(args, runner)
    return _run_await(args)


if __name__ == "__main__":
    sys.exit(main())
