"""Tests for the Stop-hook crystallization detector."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

_SCRIPT = Path(__file__).parent / "detect_crystallization_candidate.py"
_spec = importlib.util.spec_from_file_location("detect_crystallization_candidate", _SCRIPT)
assert _spec is not None and _spec.loader is not None
detect = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(detect)


def _user(text: str) -> dict[str, Any]:
    return {"type": "user", "message": {"content": [{"type": "text", "text": text}]}}


def _tool_use(name: str, block_id: str = "use-id", **input_kwargs: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": block_id, "name": name, "input": dict(input_kwargs)}


def _assistant_with_tool_uses(*tool_uses: dict[str, Any]) -> dict[str, Any]:
    return {"type": "assistant", "message": {"content": list(tool_uses)}}


def _tool_result(tool_use_id: str, *, is_error: bool = False) -> dict[str, Any]:
    return {
        "type": "user",
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": tool_use_id, "is_error": is_error}
            ]
        },
    }


def _write_transcript(tmp_path: Path, events: list[dict[str, Any]]) -> Path:
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    return path


def _empty_skills_root(tmp_path: Path) -> Path:
    skills = tmp_path / "skills"
    skills.mkdir()
    return skills


def test_evaluate_below_threshold_stays_silent(tmp_path: Path) -> None:
    events = [
        _user("hi"),
        _assistant_with_tool_uses(
            _tool_use("Bash", "u1"), _tool_use("Bash", "u2"), _tool_use("Bash", "u3")
        ),
    ]
    transcript = _write_transcript(tmp_path, events)
    should_warn, _ = detect.evaluate(
        {"transcript_path": str(transcript)}, _empty_skills_root(tmp_path)
    )
    assert should_warn is False


def test_evaluate_at_threshold_warns_with_count(tmp_path: Path) -> None:
    events = [
        _user("hi"),
        _assistant_with_tool_uses(*(_tool_use("Bash", f"u{i}") for i in range(8))),
    ]
    transcript = _write_transcript(tmp_path, events)
    should_warn, message = detect.evaluate(
        {"transcript_path": str(transcript)}, _empty_skills_root(tmp_path)
    )
    assert should_warn is True
    assert "8 non-read tool calls" in message


def test_evaluate_excludes_read_only_tools(tmp_path: Path) -> None:
    events = [
        _user("hi"),
        _assistant_with_tool_uses(
            *(_tool_use("Read", f"r{i}") for i in range(10)),
            *(_tool_use("Grep", f"g{i}") for i in range(10)),
            *(_tool_use("Glob", f"l{i}") for i in range(10)),
            _tool_use("Bash", "b1"),
        ),
    ]
    transcript = _write_transcript(tmp_path, events)
    should_warn, _ = detect.evaluate(
        {"transcript_path": str(transcript)}, _empty_skills_root(tmp_path)
    )
    assert should_warn is False


def test_evaluate_only_counts_last_turn(tmp_path: Path) -> None:
    """Earlier turns must not bleed into the count for the latest turn."""
    events = [
        _user("first turn"),
        _assistant_with_tool_uses(*(_tool_use("Bash", f"u{i}") for i in range(20))),
        _user("second turn"),
        _assistant_with_tool_uses(_tool_use("Bash", "u-new")),
    ]
    transcript = _write_transcript(tmp_path, events)
    should_warn, _ = detect.evaluate(
        {"transcript_path": str(transcript)}, _empty_skills_root(tmp_path)
    )
    assert should_warn is False


def test_evaluate_ignores_tool_result_carriers_when_finding_turn_boundary(tmp_path: Path) -> None:
    """tool_result-carrying user events must not be treated as a turn boundary."""
    events = [
        _user("real user message"),
        _assistant_with_tool_uses(*(_tool_use("Bash", f"u{i}") for i in range(8))),
        _tool_result("u0"),
        _tool_result("u1"),
    ]
    transcript = _write_transcript(tmp_path, events)
    should_warn, message = detect.evaluate(
        {"transcript_path": str(transcript)}, _empty_skills_root(tmp_path)
    )
    assert should_warn is True
    assert "8 non-read tool calls" in message


def test_evaluate_silenced_by_successful_crystallized_skill(tmp_path: Path) -> None:
    """If the turn already invoked a crystallized skill successfully, stay silent."""
    skills = _empty_skills_root(tmp_path)
    skill_dir = skills / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: x\nmetadata:\n  crystallized: true\n---\n",
        encoding="utf-8",
    )
    events = [
        _user("hi"),
        _assistant_with_tool_uses(
            *(_tool_use("Bash", f"u{i}") for i in range(10)),
            _tool_use("Skill", "skill-id", skill="my-skill"),
        ),
        _tool_result("skill-id"),
    ]
    transcript = _write_transcript(tmp_path, events)
    should_warn, _ = detect.evaluate({"transcript_path": str(transcript)}, skills)
    assert should_warn is False


def test_evaluate_not_silenced_when_crystallized_skill_errored(tmp_path: Path) -> None:
    """A failed crystallized skill call should not silence the reminder."""
    skills = _empty_skills_root(tmp_path)
    skill_dir = skills / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: x\nmetadata:\n  crystallized: true\n---\n",
        encoding="utf-8",
    )
    events = [
        _user("hi"),
        _assistant_with_tool_uses(
            *(_tool_use("Bash", f"u{i}") for i in range(10)),
            _tool_use("Skill", "skill-id", skill="my-skill"),
        ),
        _tool_result("skill-id", is_error=True),
    ]
    transcript = _write_transcript(tmp_path, events)
    should_warn, _ = detect.evaluate({"transcript_path": str(transcript)}, skills)
    assert should_warn is True


def test_evaluate_not_silenced_by_non_crystallized_skill(tmp_path: Path) -> None:
    """A successful non-crystallized skill call does not silence the reminder."""
    skills = _empty_skills_root(tmp_path)
    skill_dir = skills / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: x\n---\n",
        encoding="utf-8",
    )
    events = [
        _user("hi"),
        _assistant_with_tool_uses(
            *(_tool_use("Bash", f"u{i}") for i in range(10)),
            _tool_use("Skill", "skill-id", skill="my-skill"),
        ),
        _tool_result("skill-id"),
    ]
    transcript = _write_transcript(tmp_path, events)
    should_warn, _ = detect.evaluate({"transcript_path": str(transcript)}, skills)
    assert should_warn is True


def test_evaluate_strips_plugin_prefix_from_skill_name(tmp_path: Path) -> None:
    """`plugin:skill` invocations should resolve to the bare skill dir."""
    skills = _empty_skills_root(tmp_path)
    skill_dir = skills / "bar"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: bar\ndescription: x\nmetadata:\n  crystallized: true\n---\n",
        encoding="utf-8",
    )
    events = [
        _user("hi"),
        _assistant_with_tool_uses(
            *(_tool_use("Bash", f"u{i}") for i in range(10)),
            _tool_use("Skill", "skill-id", skill="foo:bar"),
        ),
        _tool_result("skill-id"),
    ]
    transcript = _write_transcript(tmp_path, events)
    should_warn, _ = detect.evaluate({"transcript_path": str(transcript)}, skills)
    assert should_warn is False


def test_evaluate_returns_silent_when_transcript_path_missing(tmp_path: Path) -> None:
    should_warn, _ = detect.evaluate({}, _empty_skills_root(tmp_path))
    assert should_warn is False


def test_evaluate_returns_silent_when_transcript_file_missing(tmp_path: Path) -> None:
    should_warn, _ = detect.evaluate(
        {"transcript_path": str(tmp_path / "no-such-file.jsonl")},
        _empty_skills_root(tmp_path),
    )
    assert should_warn is False


def test_evaluate_returns_silent_for_empty_transcript(tmp_path: Path) -> None:
    transcript = tmp_path / "empty.jsonl"
    transcript.write_text("", encoding="utf-8")
    should_warn, _ = detect.evaluate(
        {"transcript_path": str(transcript)}, _empty_skills_root(tmp_path)
    )
    assert should_warn is False


def test_evaluate_meta_injection_resets_tool_count(tmp_path: Path) -> None:
    """After a Stop-hook re-injection, the "turn" restarts: if the agent's next
    response has no tools, the hook must stay silent."""
    events = [
        _user("hi"),
        _assistant_with_tool_uses(*(_tool_use("Bash", f"u{i}") for i in range(8))),
        # Stop-hook fires exit=2 and Claude Code re-injects the stderr as an
        # isMeta user event. Prior tool calls are now on the far side of a
        # fresh agent-response boundary.
        {
            "type": "user",
            "isMeta": True,
            "message": {"content": [{"type": "text", "text": "Stop hook feedback: ..."}]},
        },
        # Agent replies without tools.
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Ignoring"}]}},
    ]
    transcript = _write_transcript(tmp_path, events)
    should_warn, _ = detect.evaluate(
        {"transcript_path": str(transcript)}, _empty_skills_root(tmp_path)
    )
    assert should_warn is False


def test_evaluate_refires_if_agent_keeps_using_tools_after_meta(tmp_path: Path) -> None:
    """If the agent responds to a meta injection with more tool calls past the
    threshold, that is a new qualifying turn and the hook should fire again."""
    events = [
        _user("hi"),
        _assistant_with_tool_uses(*(_tool_use("Bash", f"u{i}") for i in range(8))),
        {
            "type": "user",
            "isMeta": True,
            "message": {"content": [{"type": "text", "text": "Stop hook feedback: ..."}]},
        },
        _assistant_with_tool_uses(*(_tool_use("Bash", f"v{i}") for i in range(8))),
    ]
    transcript = _write_transcript(tmp_path, events)
    should_warn, message = detect.evaluate(
        {"transcript_path": str(transcript)}, _empty_skills_root(tmp_path)
    )
    assert should_warn is True
    assert "8 non-read tool calls" in message


def test_evaluate_tool_results_do_not_reset_count(tmp_path: Path) -> None:
    """Tool-result-carrying user events must not be treated as a response boundary."""
    events = [
        _user("hi"),
        _assistant_with_tool_uses(*(_tool_use("Bash", f"u{i}") for i in range(8))),
        _tool_result("u0"),
        _tool_result("u1"),
    ]
    transcript = _write_transcript(tmp_path, events)
    should_warn, message = detect.evaluate(
        {"transcript_path": str(transcript)}, _empty_skills_root(tmp_path)
    )
    assert should_warn is True
    assert "8 non-read tool calls" in message


def test_skill_is_crystallized_handles_missing_file(tmp_path: Path) -> None:
    assert detect._skill_is_crystallized(tmp_path / "does-not-exist.md") is False


def test_skill_is_crystallized_handles_missing_frontmatter(tmp_path: Path) -> None:
    path = tmp_path / "SKILL.md"
    path.write_text("# A skill with no frontmatter\n", encoding="utf-8")
    assert detect._skill_is_crystallized(path) is False


def test_skill_is_crystallized_detects_crystallized_true(tmp_path: Path) -> None:
    path = tmp_path / "SKILL.md"
    path.write_text(
        "---\nname: foo\ndescription: bar\nmetadata:\n  crystallized: true\n---\n",
        encoding="utf-8",
    )
    assert detect._skill_is_crystallized(path) is True


def test_skill_is_crystallized_returns_false_when_flag_absent(tmp_path: Path) -> None:
    path = tmp_path / "SKILL.md"
    path.write_text("---\nname: foo\ndescription: bar\n---\n", encoding="utf-8")
    assert detect._skill_is_crystallized(path) is False
