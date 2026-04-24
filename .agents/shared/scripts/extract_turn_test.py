"""Tests for ``resolve_transcript_path`` in ``extract_turn.py``.

Run via: ``uv run pytest .agents/shared/scripts/extract_turn_test.py``
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent / "extract_turn.py"
_spec = importlib.util.spec_from_file_location("extract_turn", _SCRIPT)
assert _spec is not None and _spec.loader is not None
extract_turn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(extract_turn)


def _write_jsonl(path: Path, events: list[dict]) -> Path:
    path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
    )
    return path


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


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


def test_state_dir_file_fallback_succeeds(tmp_path: Path) -> None:
    """With neither env var set, read session id from MNGR_AGENT_STATE_DIR/claude_session_id."""
    session_id = "session-abc"
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "claude_session_id").write_text(f"{session_id}\n", encoding="utf-8")
    projects = tmp_path / "projects"
    slug_dir = projects / "-slug"
    slug_dir.mkdir(parents=True)
    transcript = slug_dir / f"{session_id}.jsonl"
    transcript.write_text("")
    env = {
        "MNGR_AGENT_STATE_DIR": str(state_dir),
        "CLAUDE_CONFIG_DIR": str(tmp_path),
    }
    assert extract_turn.resolve_transcript_path(None, env) == transcript


def test_state_dir_file_fallback_missing_file_raises(tmp_path: Path) -> None:
    """MNGR_AGENT_STATE_DIR set but claude_session_id missing -- still raises."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    env = {"MNGR_AGENT_STATE_DIR": str(state_dir)}
    with pytest.raises(FileNotFoundError):
        extract_turn.resolve_transcript_path(None, env)


def test_session_id_env_wins_over_state_dir_file(tmp_path: Path) -> None:
    """Explicit MNGR_CLAUDE_SESSION_ID takes precedence over on-disk session id."""
    env_session = "from-env"
    file_session = "from-file"
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "claude_session_id").write_text(file_session, encoding="utf-8")
    projects = tmp_path / "projects"
    slug_dir = projects / "-slug"
    slug_dir.mkdir(parents=True)
    env_transcript = slug_dir / f"{env_session}.jsonl"
    env_transcript.write_text("")
    # Also create a transcript for the on-disk id so we can distinguish which
    # branch resolved -- if the env var wins, we should NOT pick this one.
    file_transcript = slug_dir / f"{file_session}.jsonl"
    file_transcript.write_text("")
    env = {
        "MNGR_CLAUDE_SESSION_ID": env_session,
        "MNGR_AGENT_STATE_DIR": str(state_dir),
        "CLAUDE_CONFIG_DIR": str(tmp_path),
    }
    assert extract_turn.resolve_transcript_path(None, env) == env_transcript


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


def _human(text: str) -> dict:
    return {"type": "user", "message": {"content": [{"type": "text", "text": text}]}}


def _assistant(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }


def _tool_result(tool_use_id: str) -> dict:
    return {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": tool_use_id}]},
    }


def _meta(text: str) -> dict:
    return {
        "type": "user",
        "isMeta": True,
        "message": {"content": [{"type": "text", "text": text}]},
    }


def test_cli_nth_zero_regression(tmp_path: Path) -> None:
    """--nth 0 still matches pre-change behaviour on a multi-human transcript."""
    transcript = tmp_path / "transcript.jsonl"
    events = [
        _human("A"),
        _assistant("reply-A"),
        _tool_result("t1"),
        _human("B"),
        _assistant("reply-B"),
        _tool_result("t2"),
    ]
    _write_jsonl(transcript, events)
    out = tmp_path / "out.jsonl"
    result = _run_cli("--transcript", str(transcript), "--output", str(out), "--nth", "0")
    assert result.returncode == 0, result.stderr
    assert _read_jsonl(out) == events[3:]


def test_cli_nth_one_bounds_previous_turn(tmp_path: Path) -> None:
    """--nth 1 returns slice starting at human_A and ending at human_B (exclusive)."""
    transcript = tmp_path / "transcript.jsonl"
    events = [
        _human("A"),
        _tool_result("t1"),
        _assistant("reply-A"),
        _human("B"),
        _assistant("reply-B"),
    ]
    _write_jsonl(transcript, events)
    out = tmp_path / "out.jsonl"
    result = _run_cli("--transcript", str(transcript), "--output", str(out), "--nth", "1")
    assert result.returncode == 0, result.stderr
    assert _read_jsonl(out) == events[0:3]


def test_cli_markers_slice_across_meta_injections(tmp_path: Path) -> None:
    """Marker-based slicing works even with intervening Skill-invocation pseudo-messages."""
    transcript = tmp_path / "transcript.jsonl"
    events = [
        _human("previous"),
        _assistant("did stuff"),
        _human("START-SENTINEL regen request"),
        _assistant("regen in progress"),
        _tool_result("t1"),
        _meta("Base directory for this skill: /x"),
        _assistant("more work"),
        _human("END-SENTINEL yes, crystallize it"),
        _assistant("acting on yes"),
    ]
    _write_jsonl(transcript, events)
    out = tmp_path / "out.jsonl"
    result = _run_cli(
        "--transcript",
        str(transcript),
        "--output",
        str(out),
        "--start-marker",
        "START-SENTINEL",
        "--end-marker",
        "END-SENTINEL",
    )
    assert result.returncode == 0, result.stderr
    # Slice must start at index 2 (the human "START-SENTINEL..." event) and end
    # exclusive at index 7 (the human "END-SENTINEL..." event).
    assert _read_jsonl(out) == events[2:7]


def test_cli_nth_and_start_marker_mutually_exclusive(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.jsonl"
    _write_jsonl(transcript, [_human("x")])
    out = tmp_path / "out.jsonl"
    result = _run_cli(
        "--transcript", str(transcript),
        "--output", str(out),
        "--nth", "1",
        "--start-marker", "foo",
    )
    assert result.returncode != 0
    assert "mutually exclusive" in result.stderr


def test_cli_end_marker_requires_start_marker(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.jsonl"
    _write_jsonl(transcript, [_human("x")])
    out = tmp_path / "out.jsonl"
    result = _run_cli(
        "--transcript", str(transcript),
        "--output", str(out),
        "--end-marker", "foo",
    )
    assert result.returncode != 0
    assert "requires --start-marker" in result.stderr
