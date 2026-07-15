"""Tests for the PreToolUse subprocess-tagging hook.

The hook rewrites a Bash command so the running shell raises its own
``oom_score_adj`` to the most-expendable band before the real command runs, so
an agent's build/test/browser subprocesses are shed first under memory pressure.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).parent / "claude_oom_tag_subprocess.py"
_spec = importlib.util.spec_from_file_location("claude_oom_tag_subprocess", _SCRIPT)
assert _spec is not None and _spec.loader is not None
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)

from oom_priority import bands


def test_tagged_command_runs_the_original_after_a_guarded_tag() -> None:
    tagged = hook.build_tagged_command("pytest -q")
    # The real command is preserved verbatim at the end.
    assert tagged.endswith("; pytest -q")
    # It writes the most-expendable band...
    assert str(bands.AGENT_SUBPROCESS) in tagged
    # ...gated on test -w so it cannot error where /proc is absent (e.g. macOS)...
    assert "test -w /proc/self/oom_score_adj" in tagged
    # ...and is separated with ';' (not '&&') so the command runs regardless.
    assert "; pytest -q" in tagged and "&& pytest -q" not in tagged


def test_tagging_is_a_pure_prefix_that_does_not_mangle_the_command() -> None:
    original = "cd /tmp && ./build.sh --flag 'a b'"
    tagged = hook.build_tagged_command(original)
    assert tagged.endswith("; " + original)
