"""Unit tests for the template-update prompter.

Driven with in-memory fakes: a marker file in ``tmp_path``, a capturing send,
and a settable initial-chat resolver -- no workspace or mngr involved.
"""

from pathlib import Path

from imbue.system_interface.template_update_prompter import TemplateUpdatePrompter
from imbue.system_interface.template_update_prompter import build_update_message
from imbue.system_interface.template_update_prompter import read_pending_version

_INCOMING_REF = "refs/openhost/incoming"


class _Harness:
    def __init__(self, tmp_path: Path, *, initial_chat_id: str | None = "agent-initial") -> None:
        self.marker = tmp_path / "pending"
        self.initial_chat_id = initial_chat_id
        self.sends: list[tuple[str, str]] = []
        self.send_result = True
        self.prompter = TemplateUpdatePrompter(
            send_message=self._send,
            resolve_initial_chat_agent_id=lambda: self.initial_chat_id,
            pending_marker_path=self.marker,
            incoming_ref=_INCOMING_REF,
        )

    def _send(self, agent_id: str, message: str) -> bool:
        self.sends.append((agent_id, message))
        return self.send_result


def test_no_prompt_when_marker_absent(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    assert h.prompter.check_and_prompt() is False
    assert h.sends == []


def test_prompts_initial_chat_when_marker_present(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    h.marker.write_text("v2\n")
    assert h.prompter.check_and_prompt() is True
    assert len(h.sends) == 1
    agent_id, message = h.sends[0]
    assert agent_id == "agent-initial"
    assert "v2" in message
    assert _INCOMING_REF in message


def test_prompts_only_once_per_boot(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    h.marker.write_text("v2\n")
    assert h.prompter.check_and_prompt() is True
    # Marker still present (update-self clears it on success), but we do not
    # re-send within the same boot.
    assert h.prompter.check_and_prompt() is False
    assert len(h.sends) == 1


def test_no_prompt_when_initial_chat_unresolved(tmp_path: Path) -> None:
    h = _Harness(tmp_path, initial_chat_id=None)
    h.marker.write_text("v2\n")
    assert h.prompter.check_and_prompt() is False
    assert h.sends == []


def test_failed_send_allows_retry_this_boot(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    h.marker.write_text("v2\n")
    h.send_result = False
    assert h.prompter.check_and_prompt() is False
    assert len(h.sends) == 1
    # A later send that succeeds goes through (the guard was not latched).
    h.send_result = True
    assert h.prompter.check_and_prompt() is True
    assert len(h.sends) == 2


def test_read_pending_version_handles_missing_and_empty(tmp_path: Path) -> None:
    assert read_pending_version(tmp_path / "nope") is None
    empty = tmp_path / "empty"
    empty.write_text("  \n")
    assert read_pending_version(empty) is None
    good = tmp_path / "good"
    good.write_text("abc123\n")
    assert read_pending_version(good) == "abc123"


def test_build_update_message_mentions_version_and_local_ref() -> None:
    msg = build_update_message(target_version="deadbeef", incoming_ref=_INCOMING_REF)
    assert "deadbeef" in msg
    assert _INCOMING_REF in msg
    assert "update-self" in msg
