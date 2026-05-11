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

Optional ``tk`` ticket integration: when ``--ticket-title`` is given, opens
a tracking ticket via the local ``tk`` CLI before dispatching. Failures are
printed to stderr (not silenced) and dispatch continues -- ``tk`` is
auxiliary infra, not load-bearing for the worker itself. The ticket ID is
written to ``<runtime-dir>/ticket_id.txt`` so the lead can close it later.

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
dir push lands in its worktree, the task file's ``transcript_path`` and
``lead_report_dir`` will resolve to nothing. Sending the task as a
follow-up message guarantees the worker sees the runtime dir first.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence


def _normalize_dir(value: str) -> str:
    """Return ``value`` with exactly one trailing slash."""
    return value.rstrip("/") + "/"


def open_ticket(
    title: str,
    acceptance: str,
    runner: "Runner",
) -> str | None:
    """Open and start a ``tk`` ticket; return its ID or ``None`` on failure.

    Failures (tk not installed, create/start non-zero exit) are reported on
    stderr but do not raise -- ticket tracking is auxiliary.
    """
    if not shutil.which("tk"):
        print("dispatch: tk not on PATH; skipping ticket creation", file=sys.stderr)
        return None
    try:
        created = runner.run(
            ["tk", "create", title, "-t", "task", "--acceptance", acceptance],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip() or f"exit {exc.returncode}"
        print(f"dispatch: tk create failed: {stderr}", file=sys.stderr)
        return None
    last_line = (created.stdout or "").strip().splitlines()
    ticket_id = last_line[-1].strip() if last_line else ""
    if not ticket_id:
        print("dispatch: tk create returned empty ticket ID", file=sys.stderr)
        return None
    try:
        runner.run(
            ["tk", "start", ticket_id],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip() or f"exit {exc.returncode}"
        print(f"dispatch: tk start {ticket_id} failed: {stderr}", file=sys.stderr)
    return ticket_id


def push(name: str, source_dir: str, runner: "Runner") -> None:
    """Push ``source_dir`` into worker ``name``'s worktree at the same path.

    Uses the directory form (trailing slash on both sides) and
    ``--uncommitted-changes=merge`` -- see lead-proxy.md § "mngr push
    rationale" for why both are required.
    """
    normalized = _normalize_dir(source_dir)
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


class Runner:
    """Indirection over ``subprocess.run`` so tests can intercept commands.

    The default implementation calls ``subprocess.run`` directly. Tests
    inject a recording stub instead.
    """

    def run(self, argv: Sequence[str], **kwargs):
        return subprocess.run(list(argv), **kwargs)


def dispatch(
    name: str,
    template: str,
    runtime_dir: Path,
    task_file: Path,
    extra_pushes: Sequence[Path],
    workspace: str,
    ticket_title: str | None,
    ticket_acceptance: str,
    runner: Runner | None = None,
) -> int:
    """Run the dispatch lifecycle. Returns the process exit code.

    Pre-flight checks (existence of ``runtime_dir``, ``task_file``, every
    ``extra_pushes`` entry) run first so a typo doesn't half-create a worker.
    Ticket opening (if requested) happens between pre-flight and the first
    ``mngr`` call so the ticket exists by the time the worker is created.
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

    ticket_id: str | None = None
    if ticket_title is not None:
        ticket_id = open_ticket(ticket_title, ticket_acceptance, runner)
        if ticket_id is not None:
            (runtime_dir / "ticket_id.txt").write_text(
                ticket_id + "\n", encoding="utf-8"
            )

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

    push(name, str(runtime_dir), runner)
    for extra in extra_pushes:
        push(name, str(extra), runner)

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

    if ticket_id is not None:
        print(f"dispatch: opened ticket {ticket_id}")
    print(f"dispatch: worker {name} launched and runtime pushed")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
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
    parser.add_argument(
        "--ticket-title",
        help="If given, open a tk ticket with this title before dispatching.",
    )
    parser.add_argument(
        "--ticket-acceptance",
        default="worker reached terminal status; branch merged",
        help="Acceptance criteria for the tk ticket (only used with --ticket-title).",
    )
    args = parser.parse_args(argv)

    workspace = os.environ.get("MINDS_WORKSPACE_NAME", "default")

    return dispatch(
        name=args.name,
        template=args.template,
        runtime_dir=args.runtime_dir,
        task_file=args.task_file,
        extra_pushes=tuple(args.extra_push),
        workspace=workspace,
        ticket_title=args.ticket_title,
        ticket_acceptance=args.ticket_acceptance,
    )


if __name__ == "__main__":
    sys.exit(main())
