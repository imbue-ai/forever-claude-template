"""Tests for the welcome_resend helper.

Uses `monkeypatch.setattr` to swap the injectable module-level callables
(`read_assistant_transcript`, `send_message_fn`). The ratchet count is
bumped in test_ratchets.py with rationale rather than dodged.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from imbue.system_interface import welcome_resend


def _write_welcome_skill(skill: Path) -> Path:
    skill.write_text(
        "---\nname: w\n---\n\nIntro\n\n---\n\n### Welcome to Minds\n\nA Mind\n\n---\n"
    )
    return skill


def test_strip_frontmatter_removes_top_block() -> None:
    text = "---\nname: x\n---\n\n# Body\nrest"
    assert welcome_resend._strip_frontmatter(text).startswith("\n# Body")


def test_strip_frontmatter_no_block_returns_input_unchanged() -> None:
    text = "# Body\nrest"
    assert welcome_resend._strip_frontmatter(text) == text


def test_extract_first_message_header_finds_inside_separator_block() -> None:
    body = "# Skill title\nblurb\n\n---\n\n### Welcome to Minds\n\nbody\n\n---\n"
    assert welcome_resend._extract_first_message_header(body) == "### Welcome to Minds"


def test_extract_first_message_header_returns_none_when_no_separator_block() -> None:
    assert welcome_resend._extract_first_message_header("# Just a header\nbody") is None


def test_read_welcome_opening_line_against_real_skill_file(tmp_path: Path) -> None:
    skill = _write_welcome_skill(tmp_path / "SKILL.md")
    assert welcome_resend.read_welcome_opening_line(skill) == "### Welcome to Minds"


def test_read_welcome_opening_line_falls_back_to_any_header(tmp_path: Path) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_text("---\nname: x\n---\n\n# Some other header\n\nbody\n")
    assert welcome_resend.read_welcome_opening_line(skill) == "# Some other header"


def test_transcript_shows_welcome_true_when_present() -> None:
    transcript = "Not logged in\n### Welcome to Minds\n\nA Mind runs ...\n"
    assert welcome_resend._transcript_shows_welcome(transcript, "### Welcome to Minds") is True


def test_transcript_shows_welcome_false_when_empty() -> None:
    assert welcome_resend._transcript_shows_welcome("", "### Welcome to Minds") is False
    assert welcome_resend._transcript_shows_welcome(None, "### Welcome to Minds") is False


def test_transcript_shows_welcome_false_when_only_auth_errors() -> None:
    """Auth-error assistant turns never contain the welcome opening line."""
    transcript = "Not logged in · Please run /login\nNot logged in · Please run /login"
    assert welcome_resend._transcript_shows_welcome(transcript, "### Welcome to Minds") is False


def test_check_and_resend_welcome_resends_when_transcript_missing_welcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transcript of only auth-error turns means the welcome never landed."""
    skill = _write_welcome_skill(tmp_path / "SKILL.md")
    send_calls: list[tuple[str, str]] = []

    def _record_send(name: str, message: str) -> bool:
        send_calls.append((name, message))
        return True

    monkeypatch.setattr(
        welcome_resend, "read_assistant_transcript", lambda _name: "Not logged in"
    )
    monkeypatch.setattr(welcome_resend, "send_message_fn", _record_send)

    resent = welcome_resend.check_and_resend_welcome("my-agent", skill_path=skill)
    assert resent is True
    assert send_calls == [("my-agent", "/welcome")]


def test_check_and_resend_welcome_skips_when_transcript_has_welcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delivered welcome in the transcript blocks a duplicate resend.

    This is the regression guard for the double-welcome bug: a prior
    sign-in delivered the greeting, so a later sign-in must not resend it
    even though the live tmux pane no longer shows it.
    """
    skill = _write_welcome_skill(tmp_path / "SKILL.md")
    send_calls: list[tuple[str, str]] = []

    def _record_send(name: str, message: str) -> bool:
        send_calls.append((name, message))
        return True

    monkeypatch.setattr(
        welcome_resend,
        "read_assistant_transcript",
        lambda _name: "### Welcome to Minds\n\nA Mind runs your responsibilities ...",
    )
    monkeypatch.setattr(welcome_resend, "send_message_fn", _record_send)

    resent = welcome_resend.check_and_resend_welcome("my-agent", skill_path=skill)
    assert resent is False
    assert send_calls == []


def test_check_and_resend_welcome_resends_when_transcript_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A None transcript (agent/file not found) is treated as welcome-absent."""
    skill = _write_welcome_skill(tmp_path / "SKILL.md")
    send_calls: list[tuple[str, str]] = []

    def _record_send(name: str, message: str) -> bool:
        send_calls.append((name, message))
        return True

    monkeypatch.setattr(welcome_resend, "read_assistant_transcript", lambda _name: None)
    monkeypatch.setattr(welcome_resend, "send_message_fn", _record_send)

    resent = welcome_resend.check_and_resend_welcome("my-agent", skill_path=skill)
    assert resent is True
    assert send_calls == [("my-agent", "/welcome")]


def test_check_and_resend_welcome_returns_false_when_skill_unreadable(tmp_path: Path) -> None:
    missing = tmp_path / "missing.md"
    resent = welcome_resend.check_and_resend_welcome("a", skill_path=missing)
    assert resent is False
