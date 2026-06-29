"""Tests for the welcome_resend helper.

`WelcomeResender` takes its transcript-read and message-send dependencies
as constructor arguments, so each test builds an isolated instance with
deterministic fakes -- no `unittest.mock` and no runtime attribute
patching. The resend target is resolved from the id in
`$MNGR_HOST_DIR/initial_chat_agent_id`, which tests set up with
`monkeypatch.setenv` plus a written id file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from imbue.system_interface import welcome_resend
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.welcome_resend import WelcomeResender

# A well-formed agent id (AgentId validates the `agent-<32 hex>` shape).
_AGENT_ID = "agent-00000000000000000000000000000001"


def _agent_info(agent_id: str = _AGENT_ID, name: str = "my-agent") -> AgentInfo:
    return AgentInfo(
        id=agent_id,
        name=name,
        state="RUNNING",
        agent_state_dir=Path("/tmp/agent"),
        claude_config_dir=Path("/tmp/.claude"),
    )


def _write_welcome_skill(skill: Path) -> Path:
    skill.write_text("---\nname: w\n---\n\nIntro\n\n---\n\n### Welcome to Minds\n\nA Mind\n\n---\n")
    return skill


def _set_up_host(host_dir: Path, agent_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point MNGR_HOST_DIR at `host_dir` and persist `agent_id` as the resend target.

    Mirrors the bootstrap, which writes the created chat agent's id to
    `$MNGR_HOST_DIR/initial_chat_agent_id`; `check_and_resend_welcome`
    reads it back and addresses the resend by that id.
    """
    monkeypatch.setenv("MNGR_HOST_DIR", str(host_dir))
    (host_dir / welcome_resend._INITIAL_CHAT_AGENT_ID_FILENAME).write_text(agent_id)


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


def test_resolve_initial_chat_agent_id_reads_sidecar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The resend target is the id the bootstrap persisted next to the host metadata."""
    _set_up_host(tmp_path, _AGENT_ID, monkeypatch)
    assert welcome_resend._resolve_initial_chat_agent_id() == _AGENT_ID


def test_resolve_initial_chat_agent_id_none_when_sidecar_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    assert welcome_resend._resolve_initial_chat_agent_id() is None


def test_check_and_resend_welcome_resends_when_transcript_missing_welcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transcript of only auth-error turns means the welcome never landed."""
    skill = _write_welcome_skill(tmp_path / "SKILL.md")
    _set_up_host(tmp_path, _AGENT_ID, monkeypatch)
    send_calls: list[tuple[str, str]] = []

    def _record_send(agent_id: str, message: str) -> bool:
        send_calls.append((agent_id, message))
        return True

    resender = WelcomeResender(
        resolve_agent=lambda _id: _agent_info(),
        read_assistant_transcript=lambda _agent: "Not logged in",
        send_message_fn=_record_send,
        skill_path=skill,
    )
    assert resender.check_and_resend_welcome() is True
    assert send_calls == [(_AGENT_ID, "/welcome")]


def test_check_and_resend_welcome_resolves_by_persisted_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The id read from the sidecar is the id passed to resolve_agent and sent to."""
    skill = _write_welcome_skill(tmp_path / "SKILL.md")
    _set_up_host(tmp_path, _AGENT_ID, monkeypatch)
    resolved_ids: list[str] = []
    send_calls: list[tuple[str, str]] = []

    def _record_resolve(agent_id: str) -> AgentInfo:
        resolved_ids.append(agent_id)
        return _agent_info(agent_id)

    def _record_send(agent_id: str, message: str) -> bool:
        send_calls.append((agent_id, message))
        return True

    resender = WelcomeResender(
        resolve_agent=_record_resolve,
        read_assistant_transcript=lambda _agent: "Not logged in",
        send_message_fn=_record_send,
        skill_path=skill,
    )
    assert resender.check_and_resend_welcome() is True
    assert resolved_ids == [_AGENT_ID]
    assert send_calls == [(_AGENT_ID, "/welcome")]


def test_check_and_resend_welcome_skips_when_transcript_has_welcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delivered welcome in the transcript blocks a duplicate resend.

    This is the regression guard for the double-welcome bug: a prior
    sign-in delivered the greeting, so a later sign-in must not resend it
    even though the live tmux pane no longer shows it.
    """
    skill = _write_welcome_skill(tmp_path / "SKILL.md")
    _set_up_host(tmp_path, _AGENT_ID, monkeypatch)
    send_calls: list[tuple[str, str]] = []

    def _record_send(agent_id: str, message: str) -> bool:
        send_calls.append((agent_id, message))
        return True

    resender = WelcomeResender(
        resolve_agent=lambda _id: _agent_info(),
        read_assistant_transcript=lambda _agent: "### Welcome to Minds\n\nA Mind runs ...",
        send_message_fn=_record_send,
        skill_path=skill,
    )
    assert resender.check_and_resend_welcome() is False
    assert send_calls == []


def test_check_and_resend_welcome_resends_when_transcript_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A None transcript (agent found, transcript file unreadable) is welcome-absent."""
    skill = _write_welcome_skill(tmp_path / "SKILL.md")
    _set_up_host(tmp_path, _AGENT_ID, monkeypatch)
    send_calls: list[tuple[str, str]] = []

    def _record_send(agent_id: str, message: str) -> bool:
        send_calls.append((agent_id, message))
        return True

    resender = WelcomeResender(
        resolve_agent=lambda _id: _agent_info(),
        read_assistant_transcript=lambda _agent: None,
        send_message_fn=_record_send,
        skill_path=skill,
    )
    assert resender.check_and_resend_welcome() is True
    assert send_calls == [(_AGENT_ID, "/welcome")]


def test_check_and_resend_welcome_skips_when_id_unresolved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no persisted id, the resend is skipped, not guessed."""
    skill = _write_welcome_skill(tmp_path / "SKILL.md")
    # MNGR_HOST_DIR is set but no id sidecar is written, so the target
    # agent id cannot be resolved.
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    send_calls: list[tuple[str, str]] = []

    def _record_send(agent_id: str, message: str) -> bool:
        send_calls.append((agent_id, message))
        return True

    resender = WelcomeResender(
        resolve_agent=lambda _id: _agent_info(),
        read_assistant_transcript=lambda _agent: None,
        send_message_fn=_record_send,
        skill_path=skill,
    )
    assert resender.check_and_resend_welcome() is False
    assert send_calls == []


def test_check_and_resend_welcome_skips_when_agent_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The id resolves but no agent with it exists, so the resend is skipped."""
    skill = _write_welcome_skill(tmp_path / "SKILL.md")
    _set_up_host(tmp_path, _AGENT_ID, monkeypatch)
    send_calls: list[tuple[str, str]] = []

    def _record_send(agent_id: str, message: str) -> bool:
        send_calls.append((agent_id, message))
        return True

    resender = WelcomeResender(
        resolve_agent=lambda _id: None,
        read_assistant_transcript=lambda _agent: None,
        send_message_fn=_record_send,
        skill_path=skill,
    )
    assert resender.check_and_resend_welcome() is False
    assert send_calls == []


def test_check_and_resend_welcome_returns_false_when_skill_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_up_host(tmp_path, _AGENT_ID, monkeypatch)
    resender = WelcomeResender(
        resolve_agent=lambda _id: _agent_info(),
        read_assistant_transcript=lambda _agent: None,
        send_message_fn=lambda _agent_id, _message: True,
        skill_path=tmp_path / "missing.md",
    )
    assert resender.check_and_resend_welcome() is False
