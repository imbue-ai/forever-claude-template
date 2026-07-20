"""Tests for the PreToolUse Bash-command rewrite hook.

The hook prepends, to every Bash command, an oom self-tag (so an agent's
build/test/browser subprocesses are shed first under memory pressure) and this
agent's git commit identity (so its commits are attributed to the agent, live
across a ``mngr rename``), then runs the original command verbatim.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent / "claude_rewrite_bash_command.py"
_spec = importlib.util.spec_from_file_location("claude_rewrite_bash_command", _SCRIPT)
assert _spec is not None and _spec.loader is not None
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)

from oom_priority import bands


def _clear_mngr_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "MNGR_AGENT_ID",
        "MNGR_AGENT_NAME",
        "MNGR_AGENT_STATE_DIR",
        "MNGR_HOST_DIR",
    ):
        monkeypatch.delenv(var, raising=False)


def _write_json(path: Path, data: dict[str, str]) -> None:
    path.write_text(json.dumps(data))


def test_oom_tag_prefix_is_guarded_and_runs_the_original() -> None:
    tagged = hook.build_oom_tag_prefix() + "pytest -q"
    # It writes the most-expendable band...
    assert str(bands.AGENT_SUBPROCESS) in tagged
    # ...gated on test -w so it cannot error where /proc is absent (e.g. macOS)...
    assert "test -w /proc/self/oom_score_adj" in tagged
    # ...and is separated with ';' (not '&&') so the command runs regardless.
    assert tagged.endswith("; pytest -q") and "&& pytest -q" not in tagged


def test_rewrite_is_a_pure_prefix_that_does_not_mangle_the_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_mngr_env(monkeypatch)
    original = "cd /tmp && ./build.sh --flag 'a b'"
    assert hook.build_rewritten_command(original).endswith("; " + original)


def test_no_git_prefix_when_identity_unresolvable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Outside a mngr container none of the sources exist: the git identity is
    # left to git's own resolution and only the oom tag is prepended.
    _clear_mngr_env(monkeypatch)
    rewritten = hook.build_rewritten_command("git commit -m x")
    assert "GIT_AUTHOR_NAME" not in rewritten
    assert rewritten == hook.build_oom_tag_prefix() + "git commit -m x"


def test_identity_uses_live_name_and_id_based_email(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_mngr_env(monkeypatch)
    state_dir = tmp_path / "agent"
    state_dir.mkdir()
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    # The live name comes from the state dir's data.json (a post-rename value),
    # NOT the boot-time MNGR_AGENT_NAME, which is deliberately set stale here.
    _write_json(state_dir / "data.json", {"name": "renamed-agent"})
    _write_json(host_dir / "data.json", {"host_id": "host-abc", "host_name": "box"})
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-xyz")
    monkeypatch.setenv("MNGR_AGENT_NAME", "boot-name")
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(state_dir))
    monkeypatch.setenv("MNGR_HOST_DIR", str(host_dir))

    assert hook.resolve_commit_identity() == ("renamed-agent", "agent-xyz@host-abc")


def test_identity_falls_back_to_boot_name_without_state_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_mngr_env(monkeypatch)
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    _write_json(host_dir / "data.json", {"host_id": "host-abc"})
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-xyz")
    monkeypatch.setenv("MNGR_AGENT_NAME", "boot-name")
    monkeypatch.setenv("MNGR_HOST_DIR", str(host_dir))
    # No MNGR_AGENT_STATE_DIR -> no live name -> boot-time name is used.
    assert hook.resolve_commit_identity() == ("boot-name", "agent-xyz@host-abc")


def test_identity_is_none_without_host_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # host_id has no env fallback; a missing/unreadable host data.json means we
    # can't build the routing-address email, so we emit no identity at all
    # rather than a mismatched one.
    _clear_mngr_env(monkeypatch)
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-xyz")
    monkeypatch.setenv("MNGR_AGENT_NAME", "boot-name")
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path / "missing"))
    assert hook.resolve_commit_identity() is None


def test_rewritten_command_exports_identity_for_all_four_git_vars(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_mngr_env(monkeypatch)
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    _write_json(host_dir / "data.json", {"host_id": "host-abc"})
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-xyz")
    monkeypatch.setenv("MNGR_AGENT_NAME", "sam")
    monkeypatch.setenv("MNGR_HOST_DIR", str(host_dir))

    rewritten = hook.build_rewritten_command("git commit -m x")
    # Author and committer, name and email, all point at the agent.
    assert "GIT_AUTHOR_NAME=sam" in rewritten
    assert "GIT_COMMITTER_NAME=sam" in rewritten
    assert "GIT_AUTHOR_EMAIL=agent-xyz@host-abc" in rewritten
    assert "GIT_COMMITTER_EMAIL=agent-xyz@host-abc" in rewritten
    # Uses `export ...;` so the vars reach git even inside a compound command,
    # and the original command still runs last.
    assert rewritten.startswith("export GIT_AUTHOR_NAME=")
    assert rewritten.endswith("; git commit -m x")


def test_identity_prefix_quotes_names_with_spaces() -> None:
    prefix = hook.build_commit_identity_prefix("Ada Lovelace", "agent-1@host-2")
    # A name with a space must be shell-quoted so the export parses as one value.
    assert "GIT_AUTHOR_NAME='Ada Lovelace'" in prefix
    assert prefix.startswith("export ") and prefix.endswith("; ")
