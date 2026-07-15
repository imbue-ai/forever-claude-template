#!/usr/bin/env python3
"""Service launch wrapper: tag this process's memory-shedding band from a named
service band, then exec the service command.

Used as a command prefix in ``supervisord.conf`` -- e.g.
``command=python3 scripts/oom_tag_service.py system_interface bash -c "..."`` --
so a service lands in its priority band before it (and everything it spawns)
exists. It sets its *own* ``oom_score_adj`` (the value survives ``execve`` and is
inherited across fork/exec by children), then ``exec``s the real command with its
arguments untouched. Mirrors ``claude_oom_launch.py`` (self-tag, then exec).

The first argument is a service key from ``oom_priority.bands.SERVICE_BANDS``.
Built-in services pass their own name (``system_interface``, ``cloudflared``,
...); user-created services pass ``user`` so they are shed before any built-in
service under memory pressure. An unknown key is tagged with the user-service
band (with a warning), the same as ``user``: an unrecognized service must
default to being expendable, not keep the fully-protected inherited default.

Tagging is best-effort: a failure to write ``/proc`` (e.g. macOS, which has no
``/proc``) is swallowed by ``set_oom_score_adj``. Exec is mandatory: the service
must run regardless of whether the tag stuck.

Self-contained beyond the stdlib-only ``oom_priority`` package (imported via a
``sys.path`` insert), since supervisord runs this under a plain ``python3``.
"""

import os
import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "libs" / "oom_priority" / "src")
)

from oom_priority import bands


def main() -> None:
    if len(sys.argv) < 3:
        print(
            "usage: oom_tag_service.py <service-key> <command> [args...]",
            file=sys.stderr,
        )
        sys.exit(2)
    service_key = sys.argv[1]
    command = sys.argv[2:]
    adj = bands.SERVICE_BANDS.get(service_key)
    if adj is None:
        adj = bands.USER_SERVICE
        print(
            f"oom_tag_service: unknown service band {service_key!r}; "
            "defaulting to the user-service band",
            file=sys.stderr,
        )
    bands.set_oom_score_adj(os.getpid(), adj)
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
