"""Tests for ``parse_task_frontmatter.py``.

Run via: ``uv run pytest
.agents/shared/scripts/parse_task_frontmatter_test.py``
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent / "parse_task_frontmatter.py"
_spec = importlib.util.spec_from_file_location("parse_task_frontmatter", _SCRIPT)
assert _spec is not None and _spec.loader is not None
parse_task_frontmatter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(parse_task_frontmatter)


_VALID_FRONTMATTER = """---
lead_agent: crystallize-test
finish_report_path: runtime/harden/update-foo/reports/report.md
---

# Task body
Some content here.
"""


def _write_task(tmp_path: Path, text: str) -> Path:
    task = tmp_path / "task.md"
    task.write_text(text)
    return task


def test_happy_path(tmp_path: Path) -> None:
    task = _write_task(tmp_path, _VALID_FRONTMATTER)
    result = parse_task_frontmatter.parse(task)
    assert result == {
        "lead_agent": "crystallize-test",
        "finish_report_path": "runtime/harden/update-foo/reports/report.md",
    }


def test_render_shell_evalable(tmp_path: Path) -> None:
    task = _write_task(tmp_path, _VALID_FRONTMATTER)
    fields = parse_task_frontmatter.parse(task)
    rendered = parse_task_frontmatter._render(fields)
    assert "LEAD_AGENT=crystallize-test\n" in rendered
    assert "FINISH_REPORT_PATH=runtime/harden/update-foo/reports/report.md\n" in rendered


def test_render_quotes_unsafe_values(tmp_path: Path) -> None:
    task = _write_task(
        tmp_path,
        """---
lead_agent: agent with spaces
finish_report_path: path/with$dollar/
---
body
""",
    )
    fields = parse_task_frontmatter.parse(task)
    rendered = parse_task_frontmatter._render(fields)
    # shlex.quote wraps values containing shell metachars in single quotes
    assert "LEAD_AGENT='agent with spaces'\n" in rendered
    assert "FINISH_REPORT_PATH='path/with$dollar/'\n" in rendered


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="task file not found"):
        parse_task_frontmatter.parse(tmp_path / "nope.md")


def test_no_frontmatter_delimiter(tmp_path: Path) -> None:
    task = _write_task(tmp_path, "just a plain markdown body\n")
    with pytest.raises(ValueError, match="must start with `---`"):
        parse_task_frontmatter.parse(task)


def test_unterminated_frontmatter(tmp_path: Path) -> None:
    task = _write_task(tmp_path, "---\nlead_agent: x\n")
    with pytest.raises(ValueError, match="not terminated"):
        parse_task_frontmatter.parse(task)


def test_frontmatter_not_mapping(tmp_path: Path) -> None:
    task = _write_task(tmp_path, "---\n- just\n- a\n- list\n---\nbody\n")
    with pytest.raises(ValueError, match="must be a YAML mapping"):
        parse_task_frontmatter.parse(task)


def test_invalid_yaml(tmp_path: Path) -> None:
    task = _write_task(tmp_path, "---\nlead_agent: [unbalanced\n---\nbody\n")
    with pytest.raises(ValueError, match="not valid YAML"):
        parse_task_frontmatter.parse(task)


@pytest.mark.parametrize("missing", ["lead_agent", "finish_report_path"])
def test_missing_required_field(tmp_path: Path, missing: str) -> None:
    lines = [
        "---",
        "lead_agent: a",
        "finish_report_path: b",
        "---",
        "body",
    ]
    lines = [line for line in lines if not line.startswith(f"{missing}:")]
    task = _write_task(tmp_path, "\n".join(lines) + "\n")
    with pytest.raises(ValueError, match=f"missing required field `{missing}`"):
        parse_task_frontmatter.parse(task)


def test_wrong_type_int(tmp_path: Path) -> None:
    task = _write_task(
        tmp_path,
        """---
lead_agent: 42
finish_report_path: b
---
body
""",
    )
    with pytest.raises(ValueError, match="lead_agent must be a string, got int"):
        parse_task_frontmatter.parse(task)


def test_wrong_type_list(tmp_path: Path) -> None:
    task = _write_task(
        tmp_path,
        """---
lead_agent: a
finish_report_path: [b, c]
---
body
""",
    )
    with pytest.raises(
        ValueError, match="finish_report_path must be a string, got list"
    ):
        parse_task_frontmatter.parse(task)


def test_empty_string(tmp_path: Path) -> None:
    task = _write_task(
        tmp_path,
        """---
lead_agent: a
finish_report_path: ""
---
body
""",
    )
    with pytest.raises(ValueError, match="finish_report_path must not be empty"):
        parse_task_frontmatter.parse(task)


def test_resolve_single_literal_path(tmp_path: Path) -> None:
    task = _write_task(tmp_path, _VALID_FRONTMATTER)
    assert parse_task_frontmatter.resolve(str(task)) == task


def test_resolve_single_glob_match(tmp_path: Path) -> None:
    (tmp_path / "crystallize").mkdir()
    (tmp_path / "crystallize" / "foo").mkdir()
    task = tmp_path / "crystallize" / "foo" / "task.md"
    task.write_text(_VALID_FRONTMATTER)
    pattern = str(tmp_path / "crystallize" / "*" / "task.md")
    assert parse_task_frontmatter.resolve(pattern) == task


def test_resolve_zero_matches_fails_loud(tmp_path: Path) -> None:
    pattern = str(tmp_path / "crystallize" / "*" / "task.md")
    with pytest.raises(ValueError, match="no task file matches pattern"):
        parse_task_frontmatter.resolve(pattern)


def test_resolve_missing_literal_path_fails_loud(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no task file matches pattern"):
        parse_task_frontmatter.resolve(str(tmp_path / "missing.md"))


def test_resolve_multiple_matches_fails_loud(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "task.md").write_text(_VALID_FRONTMATTER)
    (tmp_path / "b" / "task.md").write_text(_VALID_FRONTMATTER)
    pattern = str(tmp_path / "*" / "task.md")
    with pytest.raises(ValueError, match=r"pattern matches 2 files"):
        parse_task_frontmatter.resolve(pattern)


def test_extra_string_keys_pass_through(tmp_path: Path) -> None:
    task = _write_task(
        tmp_path,
        """---
lead_agent: a
finish_report_path: b
ticket_id: task-42
flow: verify
---
body
""",
    )
    result = parse_task_frontmatter.parse(task)
    assert result == {
        "lead_agent": "a",
        "finish_report_path": "b",
        "ticket_id": "task-42",
        "flow": "verify",
    }


def test_non_string_extra_keys_are_dropped(tmp_path: Path) -> None:
    """Only string values survive -- lists / mappings / numbers don't eval cleanly."""
    task = _write_task(
        tmp_path,
        """---
lead_agent: a
finish_report_path: b
nested:
  x: 1
inputs:
  - commit.diff
  - commit.log
count: 3
---
body
""",
    )
    result = parse_task_frontmatter.parse(task)
    assert set(result.keys()) == {"lead_agent", "finish_report_path"}


def test_extra_key_with_invalid_shell_identifier_fails_loud(tmp_path: Path) -> None:
    """Keys with dashes (or other shell-illegal chars) must fail loud, not silently drop."""
    task = _write_task(
        tmp_path,
        """---
lead_agent: a
finish_report_path: b
staged-inputs: commit.diff
---
body
""",
    )
    with pytest.raises(ValueError, match=r"staged-inputs.*shell identifier"):
        parse_task_frontmatter.parse(task)


def test_extra_key_starting_with_digit_fails_loud(tmp_path: Path) -> None:
    """POSIX shell identifiers cannot begin with a digit."""
    task = _write_task(
        tmp_path,
        """---
lead_agent: a
finish_report_path: b
1st_input: commit.diff
---
body
""",
    )
    with pytest.raises(ValueError, match=r"1st_input.*shell identifier"):
        parse_task_frontmatter.parse(task)


def test_render_orders_required_first_then_extras_alphabetized(tmp_path: Path) -> None:
    task = _write_task(
        tmp_path,
        """---
lead_agent: a
finish_report_path: b
ticket_id: task-42
flow: verify
---
body
""",
    )
    fields = parse_task_frontmatter.parse(task)
    rendered = parse_task_frontmatter._render(fields)
    assert rendered == (
        "LEAD_AGENT=a\nFINISH_REPORT_PATH=b\nFLOW=verify\nTICKET_ID=task-42\n"
    )
