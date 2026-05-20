#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Generic worker-dispatch driver.

Collapses the launch-task lifecycle steps (mngr create + mngr push of the
runtime dir + optional extra pushes + mngr message of the task file) into a
single invocation, so callers like ``crystallize-task`` don't have to repeat
the boilerplate.

The caller is responsible for writing the task file (with whatever YAML
frontmatter the worker template requires) and for placing it -- and any
gitignored auxiliary state -- under ``runtime/<feature>/<slug>/`` before
calling this script. ``dispatch.py`` orchestrates the lifecycle commands;
it does not compose task content.

Ticket bookkeeping (``tk create`` / ``tk start`` / ``tk close``) is the
caller's responsibility -- it lives in the calling skill's prose so each
flow can shape the ticket title, type, and acceptance criteria itself.

Lifecycle commands:

    mngr create <NAME> -t <TEMPLATE> --label workspace=<MINDS_WORKSPACE_NAME>
    mngr push   <NAME>:<RUNTIME_DIR>/   --source <RUNTIME_DIR>/
                --uncommitted-changes=merge
    mngr push   <NAME>:<EXTRA_DIR>/     --source <EXTRA_DIR>/        (per --extra-push)
                --uncommitted-changes=merge
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
from pathlib import Path
from typing import Sequence


_COMMON_TRANSCRIPT_REL = Path("commands/common_transcript.sh")


def _normalize_dir(value: str) -> str:
    """Return ``value`` with exactly one trailing slash."""
    return value.rstrip("/") + "/"


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
    """
    if state_dir is None:
        return
    script = state_dir / _COMMON_TRANSCRIPT_REL
    if not script.is_file():
        return
    runner.run([str(script), "--single-pass"], check=True)


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


def dispatch(
    name: str,
    template: str,
    runtime_dir: Path,
    task_file: Path,
    extra_pushes: Sequence[Path],
    workspace: str,
    state_dir: Path | None = None,
    runner: Runner | None = None,
) -> int:
    """Run the dispatch lifecycle. Returns the process exit code.

    Pre-flight checks (existence of ``runtime_dir``, ``task_file``, every
    ``extra_pushes`` entry) run first so a typo doesn't half-create a worker.

    ``state_dir`` is the lead's ``MNGR_AGENT_STATE_DIR``; when set, the
    converter at ``<state_dir>/commands/common_transcript.sh`` is flushed
    before the task message lands so the worker's first transcript read
    sees fresh events.
    """
    runner = runner or Runner()

    if not runtime_dir.is_dir():
        print(
            f"dispatch: --runtime-dir is not a directory: {runtime_dir}",
            file=sys.stderr,
        )
        return 2
    if not task_file.is_file():
        print(f"dispatch: --task-file not found: {task_file}", file=sys.stderr)
        return 2
    for extra in extra_pushes:
        if not extra.is_dir():
            print(
                f"dispatch: --extra-push is not a directory: {extra}", file=sys.stderr
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
    for extra in extra_pushes:
        push(name, extra, runner)

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

    print(f"dispatch: worker {name} launched and runtime pushed")
    return 0


def main(argv: Sequence[str] | None = None, runner: Runner | None = None) -> int:
    """CLI entry point. Tests inject ``runner`` to capture the argv lifecycle."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--name", required=True, help="Worker name; becomes the mngr/<name> branch."
    )
    parser.add_argument(
        "--template",
        required=True,
        help="mngr create template (e.g. 'worker', 'crystallize-worker').",
    )
    parser.add_argument(
        "--runtime-dir",
        required=True,
        type=Path,
        help="Existing runtime directory pushed verbatim into the worker's worktree.",
    )
    parser.add_argument(
        "--task-file",
        required=True,
        type=Path,
        help="Markdown task file (must already exist; typically inside --runtime-dir).",
    )
    parser.add_argument(
        "--extra-push",
        action="append",
        default=[],
        type=Path,
        metavar="DIR",
        help="Additional directory to push after --runtime-dir; pass multiple times.",
    )
    args = parser.parse_args(argv)

    workspace = os.environ.get("MINDS_WORKSPACE_NAME", "default")
    state_dir_env = os.environ.get("MNGR_AGENT_STATE_DIR")
    state_dir = Path(state_dir_env) if state_dir_env else None

    return dispatch(
        name=args.name,
        template=args.template,
        runtime_dir=args.runtime_dir,
        task_file=args.task_file,
        extra_pushes=tuple(args.extra_push),
        workspace=workspace,
        state_dir=state_dir,
        runner=runner,
    )


if __name__ == "__main__":
    sys.exit(main())
