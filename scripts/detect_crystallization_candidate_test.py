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


def _user_command(command_name: str) -> dict[str, Any]:
    """User message that invokes a slash command (e.g. ``/do-something-new``)."""
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": (
                f"<command-message>{command_name}</command-message>\n"
                f"<command-name>/{command_name}</command-name>"
            ),
        },
    }


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


def _commit_assistant(block_id: str = "commit-id") -> dict[str, Any]:
    """Assistant turn containing a single ``git commit`` Bash call."""
    return _assistant_with_tool_uses(
        _tool_use("Bash", block_id, command="git commit -m 'msg'")
    )


def _write_transcript(tmp_path: Path, events: list[dict[str, Any]]) -> Path:
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    return path


def _empty_skills_root(tmp_path: Path) -> Path:
    skills = tmp_path / "skills"
    skills.mkdir()
    return skills


def _workdir(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    return work


def test_evaluate_below_threshold_stays_silent(tmp_path: Path) -> None:
    events = [
        _user("hi"),
        _assistant_with_tool_uses(
            _tool_use("Bash", "u1"), _tool_use("Bash", "u2"), _tool_use("Bash", "u3")
        ),
    ]
    transcript = _write_transcript(tmp_path, events)
    should_warn, _ = detect.evaluate(
        {"transcript_path": str(transcript)},
        _empty_skills_root(tmp_path),
        _workdir(tmp_path),
    )
    assert should_warn is False


def test_evaluate_at_threshold_warns_with_count(tmp_path: Path) -> None:
    events = [
        _user("hi"),
        _assistant_with_tool_uses(*(_tool_use("Bash", f"u{i}") for i in range(8))),
    ]
    transcript = _write_transcript(tmp_path, events)
    should_warn, message = detect.evaluate(
        {"transcript_path": str(transcript)},
        _empty_skills_root(tmp_path),
        _workdir(tmp_path),
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
        {"transcript_path": str(transcript)},
        _empty_skills_root(tmp_path),
        _workdir(tmp_path),
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
        {"transcript_path": str(transcript)},
        _empty_skills_root(tmp_path),
        _workdir(tmp_path),
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
        {"transcript_path": str(transcript)},
        _empty_skills_root(tmp_path),
        _workdir(tmp_path),
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
    should_warn, _ = detect.evaluate(
        {"transcript_path": str(transcript)}, skills, _workdir(tmp_path)
    )
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
    should_warn, _ = detect.evaluate(
        {"transcript_path": str(transcript)}, skills, _workdir(tmp_path)
    )
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
    should_warn, _ = detect.evaluate(
        {"transcript_path": str(transcript)}, skills, _workdir(tmp_path)
    )
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
    should_warn, _ = detect.evaluate(
        {"transcript_path": str(transcript)}, skills, _workdir(tmp_path)
    )
    assert should_warn is False


def test_evaluate_returns_silent_when_transcript_path_missing(tmp_path: Path) -> None:
    should_warn, _ = detect.evaluate(
        {}, _empty_skills_root(tmp_path), _workdir(tmp_path)
    )
    assert should_warn is False


def test_evaluate_returns_silent_when_transcript_file_missing(tmp_path: Path) -> None:
    should_warn, _ = detect.evaluate(
        {"transcript_path": str(tmp_path / "no-such-file.jsonl")},
        _empty_skills_root(tmp_path),
        _workdir(tmp_path),
    )
    assert should_warn is False


def test_evaluate_returns_silent_for_empty_transcript(tmp_path: Path) -> None:
    transcript = tmp_path / "empty.jsonl"
    transcript.write_text("", encoding="utf-8")
    should_warn, _ = detect.evaluate(
        {"transcript_path": str(transcript)},
        _empty_skills_root(tmp_path),
        _workdir(tmp_path),
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
        {"transcript_path": str(transcript)},
        _empty_skills_root(tmp_path),
        _workdir(tmp_path),
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
        {"transcript_path": str(transcript)},
        _empty_skills_root(tmp_path),
        _workdir(tmp_path),
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
        {"transcript_path": str(transcript)},
        _empty_skills_root(tmp_path),
        _workdir(tmp_path),
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


# ---------------------------------------------------------------------------
# Lifecycle-skill / commit-gate suppression
# ---------------------------------------------------------------------------


def test_lifecycle_skill_invocation_suppresses_until_commit(tmp_path: Path) -> None:
    """do-something-new invoked, no commit yet -> stay silent even past threshold."""
    events = [
        _user("hi"),
        _assistant_with_tool_uses(_tool_use("Skill", "skill-id", skill="do-something-new")),
        _tool_result("skill-id"),
        _user("ok keep going"),
        _assistant_with_tool_uses(*(_tool_use("Bash", f"u{i}") for i in range(8))),
    ]
    transcript = _write_transcript(tmp_path, events)
    should_warn, _ = detect.evaluate(
        {"transcript_path": str(transcript)},
        _empty_skills_root(tmp_path),
        _workdir(tmp_path),
    )
    assert should_warn is False


def test_lifecycle_skill_then_commit_re_arms_nudge(tmp_path: Path) -> None:
    """After do-something-new + a successful commit, the nudge is allowed to fire."""
    events = [
        _user("hi"),
        _assistant_with_tool_uses(_tool_use("Skill", "skill-id", skill="do-something-new")),
        _tool_result("skill-id"),
        _user("now commit"),
        _commit_assistant("commit-id"),
        _tool_result("commit-id"),
        _user("now do more work"),
        _assistant_with_tool_uses(*(_tool_use("Bash", f"u{i}") for i in range(8))),
    ]
    transcript = _write_transcript(tmp_path, events)
    should_warn, _ = detect.evaluate(
        {"transcript_path": str(transcript)},
        _empty_skills_root(tmp_path),
        _workdir(tmp_path),
    )
    assert should_warn is True


def test_slash_command_invocation_also_suppresses(tmp_path: Path) -> None:
    """User typing /do-something-new is recognized as a lifecycle invocation."""
    events = [
        _user_command("do-something-new"),
        _assistant_with_tool_uses(*(_tool_use("Bash", f"u{i}") for i in range(8))),
    ]
    transcript = _write_transcript(tmp_path, events)
    should_warn, _ = detect.evaluate(
        {"transcript_path": str(transcript)},
        _empty_skills_root(tmp_path),
        _workdir(tmp_path),
    )
    assert should_warn is False


def test_plugin_namespaced_lifecycle_skill_recognized(tmp_path: Path) -> None:
    """`foo:do-something-new` strips the prefix and still suppresses."""
    events = [
        _user("hi"),
        _assistant_with_tool_uses(
            _tool_use("Skill", "skill-id", skill="foo:do-something-new"),
        ),
        _tool_result("skill-id"),
        _user("continue"),
        _assistant_with_tool_uses(*(_tool_use("Bash", f"u{i}") for i in range(8))),
    ]
    transcript = _write_transcript(tmp_path, events)
    should_warn, _ = detect.evaluate(
        {"transcript_path": str(transcript)},
        _empty_skills_root(tmp_path),
        _workdir(tmp_path),
    )
    assert should_warn is False


def test_errored_commit_does_not_re_arm_nudge(tmp_path: Path) -> None:
    """A failed git commit should not satisfy the commit gate after a lifecycle skill."""
    events = [
        _user("hi"),
        _assistant_with_tool_uses(_tool_use("Skill", "skill-id", skill="do-something-new")),
        _tool_result("skill-id"),
        _user("commit"),
        _commit_assistant("commit-id"),
        _tool_result("commit-id", is_error=True),
        _user("more work"),
        _assistant_with_tool_uses(*(_tool_use("Bash", f"u{i}") for i in range(8))),
    ]
    transcript = _write_transcript(tmp_path, events)
    should_warn, _ = detect.evaluate(
        {"transcript_path": str(transcript)},
        _empty_skills_root(tmp_path),
        _workdir(tmp_path),
    )
    assert should_warn is False


def test_already_nudged_for_current_commit_window_suppresses(tmp_path: Path) -> None:
    """If state file records a nudge at commit_count=N, suppress while count==N."""
    work = _workdir(tmp_path)
    state_path = work / detect.NUDGE_STATE_REL_PATH
    state_path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        _user("hi"),
        _commit_assistant("commit-id"),
        _tool_result("commit-id"),
        _user("more work"),
        _assistant_with_tool_uses(*(_tool_use("Bash", f"u{i}") for i in range(8))),
    ]
    transcript = _write_transcript(tmp_path, events)
    state_path.write_text(
        json.dumps({"transcript_path": str(transcript), "commit_count": 1}),
        encoding="utf-8",
    )
    should_warn, _ = detect.evaluate(
        {"transcript_path": str(transcript)}, _empty_skills_root(tmp_path), work
    )
    assert should_warn is False


def test_new_commit_after_nudge_re_arms(tmp_path: Path) -> None:
    """If state file recorded a nudge at count=1 and a new commit lands, fire again."""
    work = _workdir(tmp_path)
    state_path = work / detect.NUDGE_STATE_REL_PATH
    state_path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        _user("hi"),
        _commit_assistant("c1"),
        _tool_result("c1"),
        _user("more"),
        _commit_assistant("c2"),
        _tool_result("c2"),
        _user("now do work"),
        _assistant_with_tool_uses(*(_tool_use("Bash", f"u{i}") for i in range(8))),
    ]
    transcript = _write_transcript(tmp_path, events)
    state_path.write_text(
        json.dumps({"transcript_path": str(transcript), "commit_count": 1}),
        encoding="utf-8",
    )
    should_warn, _ = detect.evaluate(
        {"transcript_path": str(transcript)}, _empty_skills_root(tmp_path), work
    )
    assert should_warn is True


def test_state_file_for_different_transcript_does_not_suppress(tmp_path: Path) -> None:
    """A nudge recorded for transcript A must not gate transcript B."""
    work = _workdir(tmp_path)
    state_path = work / detect.NUDGE_STATE_REL_PATH
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"transcript_path": "/some/other/transcript.jsonl", "commit_count": 5}),
        encoding="utf-8",
    )
    events = [
        _user("hi"),
        _assistant_with_tool_uses(*(_tool_use("Bash", f"u{i}") for i in range(8))),
    ]
    transcript = _write_transcript(tmp_path, events)
    should_warn, _ = detect.evaluate(
        {"transcript_path": str(transcript)}, _empty_skills_root(tmp_path), work
    )
    assert should_warn is True


def test_nudge_persists_state_after_firing(tmp_path: Path) -> None:
    """Firing a nudge writes the state file with the current commit count."""
    work = _workdir(tmp_path)
    events = [
        _user("hi"),
        _commit_assistant("c1"),
        _tool_result("c1"),
        _user("more"),
        _assistant_with_tool_uses(*(_tool_use("Bash", f"u{i}") for i in range(8))),
    ]
    transcript = _write_transcript(tmp_path, events)
    should_warn, _ = detect.evaluate(
        {"transcript_path": str(transcript)}, _empty_skills_root(tmp_path), work
    )
    assert should_warn is True
    state = json.loads((work / detect.NUDGE_STATE_REL_PATH).read_text())
    assert state == {"transcript_path": str(transcript), "commit_count": 1}
