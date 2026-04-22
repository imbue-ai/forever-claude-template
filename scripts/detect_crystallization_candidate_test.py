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
        _assistant_with_tool_uses(*(_tool_use("Bash", f"u{i}") for i in range(5))),
    ]
    transcript = _write_transcript(tmp_path, events)
    should_warn, message = detect.evaluate(
        {"transcript_path": str(transcript)}, _empty_skills_root(tmp_path)
    )
    assert should_warn is True
    assert "5 non-read tool calls" in message


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
        _assistant_with_tool_uses(*(_tool_use("Bash", f"u{i}") for i in range(5))),
        _tool_result("u0"),
        _tool_result("u1"),
    ]
    transcript = _write_transcript(tmp_path, events)
    should_warn, message = detect.evaluate(
        {"transcript_path": str(transcript)}, _empty_skills_root(tmp_path)
    )
    assert should_warn is True
    assert "5 non-read tool calls" in message


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


def test_evaluate_does_not_refire_for_same_turn(tmp_path: Path) -> None:
    """Once a qualifying turn has fired, subsequent Stop events for it stay silent."""
    events = [
        _user("hi"),
        _assistant_with_tool_uses(*(_tool_use("Bash", f"u{i}") for i in range(8))),
    ]
    transcript = _write_transcript(tmp_path, events)
    skills = _empty_skills_root(tmp_path)
    state_dir = tmp_path / "state"
    payload = {"transcript_path": str(transcript), "session_id": "s1"}

    first_warn, _ = detect.evaluate(payload, skills, state_dir)
    second_warn, _ = detect.evaluate(payload, skills, state_dir)
    assert first_warn is True
    assert second_warn is False


def test_evaluate_refires_after_new_user_message(tmp_path: Path) -> None:
    """A new human user message resets the fire latch."""
    skills = _empty_skills_root(tmp_path)
    state_dir = tmp_path / "state"
    payload_base = {"session_id": "s1"}

    first_events = [
        _user("first"),
        _assistant_with_tool_uses(*(_tool_use("Bash", f"u{i}") for i in range(8))),
    ]
    transcript = _write_transcript(tmp_path, first_events)
    first_warn, _ = detect.evaluate(
        {**payload_base, "transcript_path": str(transcript)}, skills, state_dir
    )
    assert first_warn is True

    # Agent replies without tools; Stop fires again for the same turn -> silent.
    first_events.append(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Ignoring"}]}}
    )
    transcript = _write_transcript(tmp_path, first_events)
    repeat_warn, _ = detect.evaluate(
        {**payload_base, "transcript_path": str(transcript)}, skills, state_dir
    )
    assert repeat_warn is False

    # User sends a new message, triggering another qualifying turn.
    first_events.extend(
        [
            _user("second"),
            _assistant_with_tool_uses(*(_tool_use("Bash", f"v{i}") for i in range(8))),
        ]
    )
    transcript = _write_transcript(tmp_path, first_events)
    second_warn, _ = detect.evaluate(
        {**payload_base, "transcript_path": str(transcript)}, skills, state_dir
    )
    assert second_warn is True


def test_evaluate_dedupe_is_per_session(tmp_path: Path) -> None:
    """Different session_ids should not share the fire latch."""
    events = [
        _user("hi"),
        _assistant_with_tool_uses(*(_tool_use("Bash", f"u{i}") for i in range(8))),
    ]
    transcript = _write_transcript(tmp_path, events)
    skills = _empty_skills_root(tmp_path)
    state_dir = tmp_path / "state"

    first, _ = detect.evaluate(
        {"transcript_path": str(transcript), "session_id": "s1"}, skills, state_dir
    )
    second, _ = detect.evaluate(
        {"transcript_path": str(transcript), "session_id": "s2"}, skills, state_dir
    )
    assert first is True
    assert second is True


def test_evaluate_without_state_dir_does_not_dedupe(tmp_path: Path) -> None:
    """Legacy callers that don't pass state_dir keep the original always-fire behavior."""
    events = [
        _user("hi"),
        _assistant_with_tool_uses(*(_tool_use("Bash", f"u{i}") for i in range(8))),
    ]
    transcript = _write_transcript(tmp_path, events)
    skills = _empty_skills_root(tmp_path)
    payload = {"transcript_path": str(transcript), "session_id": "s1"}

    first, _ = detect.evaluate(payload, skills)
    second, _ = detect.evaluate(payload, skills)
    assert first is True
    assert second is True


def test_evaluate_without_session_id_does_not_dedupe(tmp_path: Path) -> None:
    """Missing session_id means we can't key the latch; prefer refire over silent drop."""
    events = [
        _user("hi"),
        _assistant_with_tool_uses(*(_tool_use("Bash", f"u{i}") for i in range(8))),
    ]
    transcript = _write_transcript(tmp_path, events)
    skills = _empty_skills_root(tmp_path)
    state_dir = tmp_path / "state"

    first, _ = detect.evaluate({"transcript_path": str(transcript)}, skills, state_dir)
    second, _ = detect.evaluate({"transcript_path": str(transcript)}, skills, state_dir)
    assert first is True
    assert second is True


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
