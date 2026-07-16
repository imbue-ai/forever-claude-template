#!/usr/bin/env python3
"""Supervisord event listener: backstop every service's memory-shedding band.

The primary tagging mechanism is the ``oom_tag_service.py`` command prefix,
which puts a service in its band at spawn, before anything it runs exists (see
``libs/oom_priority``). This listener closes the gap the prefix cannot: a
program whose command omits the prefix would keep the inherited
``oom_score_adj`` of 0 and sit as protected as sshd/supervisord -- an unknown
process must default to being expendable, not protected. Subscribed to
``PROCESS_STATE_RUNNING`` (fired at boot and on every restart), it resolves each
program's expected band (``bands.supervisord_program_band``: a built-in's own
band, the user-service band for anything unrecognized) and raises the process up
to it.

The RUNNING event fires only after a program has stayed up ``startsecs`` (~1s),
so the process may already have spawned children that inherited its untagged
value; those are found via a ``/proc/<pid>/task/*/children`` walk and raised
too. Children spawned after the tag inherit it.

The write is raise-only -- it makes a process more expendable, never less: a
process that tagged itself higher (the shared browser at the ceiling) is left
alone, and programs whose expected band is ``PROTECTED`` (earlyoom, this
listener itself) are never touched. Raising is also the direction that needs no
capability.

stdout carries the supervisord event-listener protocol (READY / RESULT
handshakes) and nothing else; diagnostics go to stderr. Every event is answered
OK: tagging is best-effort, and a FAIL would only make supervisord rebuffer and
replay an event we can do nothing more with.

Self-contained beyond the stdlib-only ``oom_priority`` package (imported via a
``sys.path`` insert), since supervisord runs this under a plain ``python3``.
"""

import sys
from collections.abc import Callable, Iterable
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "libs" / "oom_priority" / "src")
)

from oom_priority import bands
from oom_priority.proctree import list_descendant_pids


def parse_token_fields(line: str) -> dict[str, str]:
    """Parse a supervisord header/payload line of space-separated ``key:value``
    tokens into a dict. Tokens without a colon are ignored."""
    fields: dict[str, str] = {}
    for token in line.split():
        key, sep, value = token.partition(":")
        if sep:
            fields[key] = value
    return fields


def raise_pids_to_band(
    pids: Iterable[int],
    band: int,
    read_adj: Callable[[int], int | None] = bands.read_oom_score_adj,
    write_adj: Callable[[int, int], bool] = bands.set_oom_score_adj,
) -> None:
    """Raise each pid's ``oom_score_adj`` up to ``band``; never lower one.

    A pid whose current value is unreadable (it exited, or there is no ``/proc``)
    is skipped, as is one already at or above the band (e.g. self-tagged).
    """
    for pid in pids:
        current = read_adj(pid)
        if current is None or current >= band:
            continue
        write_adj(pid, band)


def handle_running_event(
    payload: str,
    read_adj: Callable[[int], int | None] = bands.read_oom_score_adj,
    write_adj: Callable[[int, int], bool] = bands.set_oom_score_adj,
    list_descendants: Callable[[int], list[int]] = list_descendant_pids,
) -> None:
    """Backstop-tag the program a ``PROCESS_STATE_RUNNING`` payload describes.

    The collaborators are injectable so the policy (which pids get which band)
    is testable without a real process tree.
    """
    fields = parse_token_fields(payload)
    program_name = fields.get("processname", "")
    pid_text = fields.get("pid", "")
    if not program_name or not pid_text.isdigit():
        return
    band = bands.supervisord_program_band(program_name)
    if band <= bands.PROTECTED:
        return
    pid = int(pid_text)
    raise_pids_to_band([pid, *list_descendants(pid)], band, read_adj, write_adj)


def _write_protocol(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def _serve_one_event() -> bool:
    """One READY -> event -> RESULT protocol round. Returns False on EOF
    (supervisord closed the pipe; exit so it can restart us cleanly)."""
    _write_protocol("READY\n")
    header_line = sys.stdin.readline()
    if not header_line:
        return False
    header = parse_token_fields(header_line)
    length_text = header.get("len", "")
    payload = sys.stdin.read(int(length_text)) if length_text.isdigit() else ""
    try:
        if header.get("eventname") == "PROCESS_STATE_RUNNING":
            handle_running_event(payload)
    except Exception as error:
        print(f"oom_tag_backstop: event skipped: {error}", file=sys.stderr)
    _write_protocol("RESULT 2\nOK")
    return True


def main() -> None:
    while _serve_one_event():
        pass


if __name__ == "__main__":
    main()
