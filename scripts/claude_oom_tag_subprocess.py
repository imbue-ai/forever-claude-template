#!/usr/bin/env python3
"""PreToolUse hook: tag an agent's Bash subprocesses as the most-expendable work.

A claude agent runs shell commands through the Bash tool. Those subprocesses
(builds, test runs, browsers) are exactly what should be shed first under memory
pressure -- before the agent itself, and before any other agent. They inherit
the agent's own band by default, which is not expendable enough, so this hook
rewrites each Bash command to raise the running shell's ``oom_score_adj`` to the
most-expendable band before the real command runs. Everything the shell spawns
inherits it.

Raising ``oom_score_adj`` is unprivileged, so the prepended write needs no
special capability. The write is gated on ``test -w`` so that on a host without
a writable ``/proc/self/oom_score_adj`` (e.g. macOS, which has no ``/proc``) the
prefix is a clean no-op that emits nothing -- a bare ``> /proc/...`` redirect
would otherwise leak a shell "no such file or directory" error past
``2>/dev/null``. It is separated from the real command with ``;`` (not ``&&``) so
the command runs regardless of whether the tag was applied.

This must be the LAST PreToolUse hook in the chain so the earlier inspection
hooks (commit-guard, tk-standalone, step-gate) see the original command, not the
rewritten one.

Output contract (Claude Code hooks): for a Bash call, print a JSON object with
``hookSpecificOutput.updatedInput.command`` set to the rewritten command. For any
other tool, or a malformed payload, print nothing and exit 0 (pass through).

Self-contained beyond the stdlib-only ``oom_priority`` package (imported via a
``sys.path`` insert), since claude runs PreToolUse hooks under a plain
``python3``.
"""

import json
import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "libs" / "oom_priority" / "src")
)

from oom_priority import bands


def build_tagged_command(command: str) -> str:
    """Prepend the self-tagging write to ``command``.

    ``test -w`` gates the redirect so the prefix emits nothing and cannot fail on
    a host where ``/proc/self/oom_score_adj`` is absent or not writable.
    """
    adj = bands.AGENT_SUBPROCESS
    prefix = (
        f"test -w /proc/self/oom_score_adj && "
        f"echo {adj} > /proc/self/oom_score_adj 2>/dev/null; "
    )
    return prefix + command


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return
    if not isinstance(payload, dict):
        return
    if payload.get("tool_name") != "Bash":
        return
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return
    command = tool_input.get("command")
    if not isinstance(command, str) or not command:
        return
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": {**tool_input, "command": build_tagged_command(command)},
        }
    }
    json.dump(output, sys.stdout)


if __name__ == "__main__":
    main()
