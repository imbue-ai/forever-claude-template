#!/usr/bin/env python3
"""PreToolUse hook: rewrite each Bash command before it runs.

A claude agent runs shell commands through the Bash tool. This hook rewrites
every such command by prepending two things, then runs the original command
verbatim at the end:

1. An oom self-tag. The agent's subprocesses (builds, test runs, browsers) are
   exactly what should be shed first under memory pressure -- before the agent
   itself, and before any other agent. They inherit the agent's own band by
   default, which is not expendable enough, so the prefix raises the running
   shell's ``oom_score_adj`` to the most-expendable band; everything the shell
   spawns inherits it. Raising ``oom_score_adj`` is unprivileged, so it needs no
   special capability. The write is gated on ``test -w`` so that on a host
   without a writable ``/proc/self/oom_score_adj`` (e.g. macOS, which has no
   ``/proc``) the prefix is a clean no-op that emits nothing -- a bare
   ``> /proc/...`` redirect would otherwise leak a shell "no such file or
   directory" error past ``2>/dev/null``.

2. This agent's git commit identity, as ``GIT_AUTHOR_*`` / ``GIT_COMMITTER_*``
   exports. git resolves author/committer from these env vars ahead of any
   config, so every commit the agent makes through the Bash tool is attributed
   to the agent rather than to whatever ``user.name`` the checkout inherited at
   create time (which is the operator's, copied from the source host). The name
   is the agent's *current* mngr name, read live so a ``mngr rename`` is
   reflected on the next commit without restarting the agent; the email is the
   agent's mngr routing address in id form (``<agent_id>@<host_id>``), which is
   stable across renames and still routes via ``mngr message``. When the
   identity can't be fully resolved (e.g. run outside a mngr container), the
   git prefix is omitted and git falls back to its own resolution.

Each prefix ends with ``;`` (not ``&&``) so the original command runs regardless
of whether the tag applied. The exports use ``export`` (not an inline
``VAR=x cmd`` assignment) so they reach a ``git`` invoked anywhere inside a
compound command, not just a bare leading one.

This must remain the single PreToolUse hook that rewrites the command (the only
one emitting ``updatedInput``), so the inspection hooks (commit-guard,
tk-standalone, step-gate) always act on the original command. Claude Code runs a
matcher's hooks in parallel with no defined reconciliation of multiple
``updatedInput`` outputs, so a second rewriter would nondeterministically clobber
this one -- fold any further command rewriting in here rather than adding one.

Output contract (Claude Code hooks): for a Bash call, print a JSON object with
``hookSpecificOutput.updatedInput.command`` set to the rewritten command. For any
other tool, or a malformed payload, print nothing and exit 0 (pass through).

Self-contained beyond the stdlib-only ``oom_priority`` package (imported via a
``sys.path`` insert), since claude runs PreToolUse hooks under a plain
``python3``.
"""

import json
import os
import shlex
import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "libs" / "oom_priority" / "src")
)

from oom_priority import bands

_AGENT_ID_ENV_VAR = "MNGR_AGENT_ID"
_AGENT_NAME_ENV_VAR = "MNGR_AGENT_NAME"
_AGENT_STATE_DIR_ENV_VAR = "MNGR_AGENT_STATE_DIR"
_HOST_DIR_ENV_VAR = "MNGR_HOST_DIR"


def _read_json_str_field(path: Path, field: str) -> str | None:
    """Return ``field`` from the JSON object at ``path``, or None if unavailable.

    Any read/parse failure, a non-object payload, or a missing/empty/non-string
    field yields None so a caller can fall back rather than raising.
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get(field)
    return value if isinstance(value, str) and value else None


def resolve_commit_identity() -> tuple[str, str] | None:
    """Resolve ``(author_name, author_email)`` for this agent, or None.

    The name is the agent's current mngr name -- read live from the agent state
    dir's ``data.json`` so a ``mngr rename`` is reflected without restarting the
    agent -- falling back to the boot-time ``MNGR_AGENT_NAME``. The email is the
    agent's mngr routing address in id form, ``<agent_id>@<host_id>``, which is
    rename-stable and still routes via ``mngr message``. Returns None when any
    component is missing, so a partial or mismatched identity is never emitted.
    """
    agent_id = os.environ.get(_AGENT_ID_ENV_VAR)

    host_dir = os.environ.get(_HOST_DIR_ENV_VAR)
    host_id = (
        _read_json_str_field(Path(host_dir) / "data.json", "host_id")
        if host_dir
        else None
    )

    name: str | None = None
    state_dir = os.environ.get(_AGENT_STATE_DIR_ENV_VAR)
    if state_dir:
        name = _read_json_str_field(Path(state_dir) / "data.json", "name")
    if not name:
        name = os.environ.get(_AGENT_NAME_ENV_VAR)

    if not (name and agent_id and host_id):
        return None
    return name, f"{agent_id}@{host_id}"


def build_commit_identity_prefix(author_name: str, author_email: str) -> str:
    """Build the ``export GIT_AUTHOR_*/GIT_COMMITTER_*; `` command prefix."""
    assignments = " ".join(
        f"{var}={shlex.quote(value)}"
        for var, value in (
            ("GIT_AUTHOR_NAME", author_name),
            ("GIT_COMMITTER_NAME", author_name),
            ("GIT_AUTHOR_EMAIL", author_email),
            ("GIT_COMMITTER_EMAIL", author_email),
        )
    )
    return f"export {assignments}; "


def build_oom_tag_prefix() -> str:
    """Build the guarded ``oom_score_adj`` self-tag command prefix."""
    adj = bands.AGENT_SUBPROCESS
    return (
        f"test -w /proc/self/oom_score_adj && "
        f"echo {adj} > /proc/self/oom_score_adj 2>/dev/null; "
    )


def build_rewritten_command(command: str) -> str:
    """Prepend the commit-identity (if resolvable) and oom-tag prefixes.

    ``command`` is preserved verbatim at the end; both prefixes are guarded or
    ignorable so it runs regardless of whether either applied.
    """
    prefix = build_oom_tag_prefix()
    identity = resolve_commit_identity()
    if identity is not None:
        prefix = build_commit_identity_prefix(*identity) + prefix
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
            "updatedInput": {**tool_input, "command": build_rewritten_command(command)},
        }
    }
    json.dump(output, sys.stdout)


if __name__ == "__main__":
    main()
