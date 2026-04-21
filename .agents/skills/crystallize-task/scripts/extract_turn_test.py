"""Tests for ``resolve_transcript_path`` in ``extract_turn.py``.

Run via: ``uv run pytest .agents/skills/crystallize-task/scripts/extract_turn_test.py``
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent / "extract_turn.py"
_spec = importlib.util.spec_from_file_location("extract_turn", _SCRIPT)
assert _spec is not None and _spec.loader is not None
extract_turn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(extract_turn)


def test_explicit_path_wins(tmp_path: Path) -> None:
    explicit = tmp_path / "t.jsonl"
    explicit.write_text("")
    env = {
        "CLAUDE_TRANSCRIPT_PATH": "/should/be/ignored.jsonl",
        "MNGR_CLAUDE_SESSION_ID": "abc",
        "CLAUDE_CONFIG_DIR": str(tmp_path / "unused"),
    }
    assert extract_turn.resolve_transcript_path(explicit, env) == explicit


def test_claude_transcript_path_env(tmp_path: Path) -> None:
    hook_path = tmp_path / "hook.jsonl"
    env = {"CLAUDE_TRANSCRIPT_PATH": str(hook_path)}
    assert extract_turn.resolve_transcript_path(None, env) == hook_path


def test_session_id_lookup_succeeds(tmp_path: Path) -> None:
    session_id = "session-xyz"
    projects = tmp_path / "projects"
    slug_dir = projects / "-some-slug"
    slug_dir.mkdir(parents=True)
    transcript = slug_dir / f"{session_id}.jsonl"
    transcript.write_text("")
    env = {
        "MNGR_CLAUDE_SESSION_ID": session_id,
        "CLAUDE_CONFIG_DIR": str(tmp_path),
    }
    assert extract_turn.resolve_transcript_path(None, env) == transcript


def test_session_id_prefers_non_subagent(tmp_path: Path) -> None:
    session_id = "session-xyz"
    projects = tmp_path / "projects"
    slug_dir = projects / "-slug"
    slug_dir.mkdir(parents=True)
    primary = slug_dir / f"{session_id}.jsonl"
    primary.write_text("")
    subagents = slug_dir / "parent" / "subagents"
    subagents.mkdir(parents=True)
    subagent = subagents / f"{session_id}.jsonl"
    subagent.write_text("")
    env = {
        "MNGR_CLAUDE_SESSION_ID": session_id,
        "CLAUDE_CONFIG_DIR": str(tmp_path),
    }
    assert extract_turn.resolve_transcript_path(None, env) == primary


def test_no_env_no_explicit_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        extract_turn.resolve_transcript_path(None, {})


def test_session_id_without_match_raises(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    projects.mkdir()
    env = {
        "MNGR_CLAUDE_SESSION_ID": "missing",
        "CLAUDE_CONFIG_DIR": str(tmp_path),
    }
    with pytest.raises(FileNotFoundError):
        extract_turn.resolve_transcript_path(None, env)


def test_projects_dir_missing_raises(tmp_path: Path) -> None:
    env = {
        "MNGR_CLAUDE_SESSION_ID": "abc",
        "CLAUDE_CONFIG_DIR": str(tmp_path / "does-not-exist"),
    }
    with pytest.raises(FileNotFoundError):
        extract_turn.resolve_transcript_path(None, env)
