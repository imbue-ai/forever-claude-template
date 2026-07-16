"""Tests for the OOM band backstop event listener.

The policy pieces (payload parsing, the raise-only write) are tested with
injected collaborators, so no real process tree or writable ``/proc`` is needed
(the descendant walk itself is tested with ``oom_priority.proctree``). The
event-listener protocol loop is exercised end to end via a subprocess fed a
scripted event stream; the scripted event names a PROTECTED program so the loop
never touches the real ``/proc`` on a Linux runner.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import oom_tag_backstop
from oom_priority import bands

_SCRIPT = Path(__file__).parent / "oom_tag_backstop.py"


def _running_payload(program_name: str, pid: int) -> str:
    return f"processname:{program_name} groupname:{program_name} from_state:STARTING pid:{pid}"


class _FakeProc:
    """Records adj reads/writes against a fixed initial state."""

    def __init__(self, initial: dict[int, int]) -> None:
        self.adj = dict(initial)
        self.writes: list[tuple[int, int]] = []

    def read(self, pid: int) -> int | None:
        return self.adj.get(pid)

    def write(self, pid: int, adj: int) -> bool:
        self.adj[pid] = adj
        self.writes.append((pid, adj))
        return True


def test_unknown_program_and_its_children_are_raised_to_the_user_service_band() -> None:
    proc = _FakeProc({100: 0, 101: 0})
    oom_tag_backstop.handle_running_event(
        _running_payload("my-cool-service", 100),
        read_adj=proc.read,
        write_adj=proc.write,
        list_descendants=lambda pid: [101],
    )
    assert proc.writes == [(100, bands.USER_SERVICE), (101, bands.USER_SERVICE)]


def test_builtin_program_missing_its_prefix_is_raised_to_its_own_band() -> None:
    proc = _FakeProc({200: 0})
    oom_tag_backstop.handle_running_event(
        _running_payload("cloudflared", 200),
        read_adj=proc.read,
        write_adj=proc.write,
        list_descendants=lambda pid: [],
    )
    assert proc.writes == [(200, bands.SERVICE_BANDS["cloudflared"])]


def test_never_lowers_a_self_tagged_process() -> None:
    # The browser tags itself to the ceiling at spawn; a correctly-prefixed
    # process is already at its band. Neither may be lowered (or re-written).
    proc = _FakeProc({300: bands.SHARED_BROWSER, 301: bands.SERVICE_BANDS["web"]})
    oom_tag_backstop.handle_running_event(
        _running_payload("browser", 300),
        read_adj=proc.read,
        write_adj=proc.write,
        list_descendants=lambda pid: [],
    )
    oom_tag_backstop.handle_running_event(
        _running_payload("web", 301),
        read_adj=proc.read,
        write_adj=proc.write,
        list_descendants=lambda pid: [],
    )
    assert proc.writes == []


def test_protected_programs_are_never_touched() -> None:
    proc = _FakeProc({400: 0})
    for program_name in ("earlyoom", "oom-tag-backstop", "deferred-install"):
        oom_tag_backstop.handle_running_event(
            _running_payload(program_name, 400),
            read_adj=proc.read,
            write_adj=proc.write,
            list_descendants=lambda pid: [],
        )
    assert proc.writes == []


def test_exited_processes_are_skipped() -> None:
    # pid 501 exited between the event and the walk: its adj is unreadable.
    proc = _FakeProc({500: 0})
    oom_tag_backstop.handle_running_event(
        _running_payload("my-cool-service", 500),
        read_adj=proc.read,
        write_adj=proc.write,
        list_descendants=lambda pid: [501],
    )
    assert proc.writes == [(500, bands.USER_SERVICE)]


def test_malformed_payload_is_ignored() -> None:
    proc = _FakeProc({600: 0})
    for payload in ("", "pid:600", "processname:x pid:not-a-pid", "garbage"):
        oom_tag_backstop.handle_running_event(
            payload,
            read_adj=proc.read,
            write_adj=proc.write,
            list_descendants=lambda pid: [],
        )
    assert proc.writes == []


def test_protocol_round_trip() -> None:
    # Two scripted events: a PROTECTED program (never touches /proc even on a
    # Linux runner) and a non-RUNNING event type that must be acknowledged but
    # not handled. The listener must answer READY / RESULT for both, then exit
    # cleanly on EOF.
    payload = _running_payload("earlyoom", 1)
    header = (
        "ver:3.0 server:supervisor serial:1 pool:oom-tag-backstop poolserial:1 "
        f"eventname:PROCESS_STATE_RUNNING len:{len(payload)}\n"
    )
    other_payload = "processname:web groupname:web from_state:RUNNING pid:1"
    other_header = (
        "ver:3.0 server:supervisor serial:2 pool:oom-tag-backstop poolserial:2 "
        f"eventname:PROCESS_STATE_STOPPING len:{len(other_payload)}\n"
    )
    result = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        input=header + payload + other_header + other_payload,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "READY\nRESULT 2\nOKREADY\nRESULT 2\nOKREADY\n"
