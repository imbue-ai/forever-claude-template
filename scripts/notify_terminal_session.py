#!/usr/bin/env python3
"""Best-effort notifier: tells system_interface that a terminal's tmux session changed.

Invoked by the workspace tmux ``client-session-changed`` and ``session-renamed``
hooks. It gathers the affected session name/id from tmux directly (rather than
receiving them as arguments -- a tmux session id like ``$3`` would be mangled if
passed through the hook's ``sh -c``), then POSTs the change to the
system_interface loopback endpoint, which re-broadcasts it to the dockview UI so
the affected terminal tab's title tracks the live session.

Standard library only; every network / OS / tmux error is swallowed: this runs
inside a tmux hook and must never fail in a way that disrupts tmux.
"""

import argparse
import json
import subprocess
import urllib.request

_NOTIFY_URL = "http://localhost:8000/api/terminals/notify"
_TIMEOUT_SECONDS = 2.0


def _post(payload: dict[str, str]) -> None:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        _NOTIFY_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # urllib.error.URLError (an OSError subclass) covers connection-refused /
    # timeout when the server isn't up yet; swallow so the hook stays quiet.
    try:
        urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS).close()
    except OSError:
        pass


def _run_tmux(arguments: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["tmux", *arguments],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _notify_session_changed(client_tty: str) -> None:
    if not client_tty:
        return
    output = _run_tmux(
        ["list-clients", "-F", "#{client_tty}\t#{session_name}\t#{session_id}"]
    )
    if output is None:
        return
    for line in output.splitlines():
        fields = line.split("\t", 2)
        if len(fields) < 3:
            continue
        tty, session_name, session_id = fields
        if tty == client_tty:
            _post(
                {
                    "kind": "session-changed",
                    "client_tty": client_tty,
                    "session_name": session_name,
                    "session_id": session_id,
                }
            )
            return


def _notify_session_renamed() -> None:
    # Renames are rare and carry no client context, so enumerate every session
    # and let the frontend match the affected tab by session id.
    output = _run_tmux(["list-sessions", "-F", "#{session_id}\t#{session_name}"])
    if output is None:
        return
    for line in output.splitlines():
        fields = line.split("\t", 1)
        if len(fields) < 2:
            continue
        session_id, session_name = fields
        _post(
            {
                "kind": "session-renamed",
                "client_tty": "",
                "session_name": session_name,
                "session_id": session_id,
            }
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Notify system_interface of a terminal tmux session change."
    )
    parser.add_argument(
        "--kind", required=True, help="Either 'session-changed' or 'session-renamed'."
    )
    parser.add_argument(
        "--client-tty",
        default="",
        help="The tmux client's tty (pty), for session-changed.",
    )
    arguments = parser.parse_args()

    if arguments.kind == "session-changed":
        _notify_session_changed(arguments.client_tty)
    elif arguments.kind == "session-renamed":
        _notify_session_renamed()


if __name__ == "__main__":
    main()
