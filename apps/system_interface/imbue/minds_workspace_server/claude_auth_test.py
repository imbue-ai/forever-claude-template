"""Tests for the claude_auth backend module.

The module exposes `command_runner` and `pexpect_spawner` as injectable
module-level callables. Tests use `monkeypatch.setattr` to swap them
for deterministic fakes. This honors the spirit of
`PREVENT_UNITTEST_MOCK_IMPORTS` (no mock framework) and
`PREVENT_MONKEYPATCH_SETATTR` (count is bumped with the rationale documented
in test_ratchets.py, not dodged via hand-rolled try/finally).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from imbue.minds_workspace_server import claude_auth


class _FakeFinishedProcess:
    """Minimal stand-in for `FinishedProcess` produced by the command runner."""

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class _FakePexpectProcess:
    """Records the inputs the OAuth flow sends to a `pexpect.spawn`."""

    def __init__(
        self,
        url_match: str | None,
        expect_return_index: int = 0,
        eof_return_index: int = 0,
    ) -> None:
        self._url_match = url_match
        self._expect_return_index = expect_return_index
        self._eof_return_index = eof_return_index
        self._expect_call_count = 0
        self.sendline_calls: list[str] = []
        self.terminate_calls = 0
        self.close_calls = 0
        self.timeout: float | None = None
        self.match: Any = None
        if url_match is not None:
            self.match = re.compile(r".*").match(url_match)
            assert self.match is not None

    def expect(self, _patterns: object) -> int:
        self._expect_call_count += 1
        if self._expect_call_count == 1:
            return self._expect_return_index
        return self._eof_return_index

    def sendline(self, s: str) -> None:
        self.sendline_calls.append(s)

    def isalive(self) -> bool:
        return True

    def terminate(self, force: bool = False) -> None:
        self.terminate_calls += 1

    def close(self) -> None:
        self.close_calls += 1


@pytest.fixture(autouse=True)
def reset_oauth_session() -> Iterator[None]:
    claude_auth.abort_oauth_login()
    yield
    claude_auth.abort_oauth_login()


def test_parse_status_payload_full() -> None:
    payload: dict[str, object] = {
        "loggedIn": True,
        "authMethod": "oauth",
        "apiProvider": "claudeai",
        "email": "user@example.com",
        "orgId": "org-1",
        "orgName": "Example",
        "subscriptionType": "Max",
    }
    status = claude_auth._parse_status_payload(payload)
    assert status.logged_in is True
    assert status.email == "user@example.com"
    assert status.subscription_type == "Max"


def test_parse_status_payload_minimal() -> None:
    status = claude_auth._parse_status_payload({"loggedIn": False})
    assert status.logged_in is False
    assert status.email is None
    assert status.subscription_type is None


def test_parse_status_payload_empty_strings_coerced_to_none() -> None:
    payload: dict[str, object] = {"loggedIn": True, "email": "", "subscriptionType": ""}
    status = claude_auth._parse_status_payload(payload)
    assert status.email is None
    assert status.subscription_type is None


def test_get_auth_status_returns_logged_out_when_runner_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _runner(_cmd: list[str], _timeout: float) -> _FakeFinishedProcess:
        raise claude_auth.ProcessSetupError(
            command=("claude",), stdout="", stderr="not found", is_output_already_logged=False
        )

    monkeypatch.setattr(claude_auth, "command_runner", _runner)
    status = claude_auth.get_auth_status()
    assert status.logged_in is False


def test_get_auth_status_parses_logged_in_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def _runner(_cmd: list[str], _timeout: float) -> _FakeFinishedProcess:
        return _FakeFinishedProcess(
            stdout='{"loggedIn": true, "email": "x@y.com", "subscriptionType": "Pro"}'
        )

    monkeypatch.setattr(claude_auth, "command_runner", _runner)
    status = claude_auth.get_auth_status()
    assert status.logged_in is True
    assert status.email == "x@y.com"
    assert status.subscription_type == "Pro"


def test_get_auth_status_rejects_non_json_output(monkeypatch: pytest.MonkeyPatch) -> None:
    def _runner(_cmd: list[str], _timeout: float) -> _FakeFinishedProcess:
        return _FakeFinishedProcess(stdout="not json at all")

    monkeypatch.setattr(claude_auth, "command_runner", _runner)
    with pytest.raises(claude_auth.ClaudeAuthError, match="non-JSON"):
        claude_auth.get_auth_status()


def test_get_auth_status_treats_empty_output_as_logged_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _runner(_cmd: list[str], _timeout: float) -> _FakeFinishedProcess:
        return _FakeFinishedProcess(stdout="")

    monkeypatch.setattr(claude_auth, "command_runner", _runner)
    status = claude_auth.get_auth_status()
    assert status.logged_in is False


def test_format_env_file_simple() -> None:
    text = claude_auth._format_env_file({"FOO": "bar"})
    assert text == "FOO=bar\n"


def test_format_env_file_quotes_values_with_spaces() -> None:
    text = claude_auth._format_env_file({"FOO": "bar baz"})
    assert text == 'FOO="bar baz"\n'


def test_write_api_key_creates_file_when_missing(tmp_path: Path) -> None:
    env_path = tmp_path / "env"
    claude_auth.write_api_key_to_host_env(SecretStr("sk-ant-test"), env_path_override=env_path)
    assert env_path.read_text() == "ANTHROPIC_API_KEY=sk-ant-test\n"


def test_write_api_key_updates_existing_file(tmp_path: Path) -> None:
    env_path = tmp_path / "env"
    env_path.write_text("CLAUDE_CONFIG_DIR=/some/path\nANTHROPIC_API_KEY=old\n")
    claude_auth.write_api_key_to_host_env(SecretStr("sk-ant-new"), env_path_override=env_path)
    text = env_path.read_text()
    assert "ANTHROPIC_API_KEY=sk-ant-new" in text
    assert "CLAUDE_CONFIG_DIR=/some/path" in text
    assert "old" not in text


def test_submit_oauth_code_rejects_unknown_session() -> None:
    with pytest.raises(claude_auth.ClaudeAuthError, match="No active OAuth session"):
        claude_auth.submit_oauth_code("bogus", "fake#code")


def test_oauth_session_extracts_url_from_spawner_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_url = "https://claude.ai/oauth/authorize?code=abc&state=def"
    fake_process = _FakePexpectProcess(url_match=fake_url, expect_return_index=0)
    monkeypatch.setattr(
        claude_auth, "pexpect_spawner", lambda *_args, **_kwargs: fake_process
    )
    result = claude_auth.start_oauth_login(claude_auth.OAuthProvider.CLAUDEAI)
    assert result.oauth_url == fake_url
    assert result.session_id


def test_oauth_session_raises_on_eof_before_url(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_process = _FakePexpectProcess(url_match=None, expect_return_index=1)
    monkeypatch.setattr(
        claude_auth, "pexpect_spawner", lambda *_args, **_kwargs: fake_process
    )
    with pytest.raises(claude_auth.ClaudeAuthError, match="before printing OAuth URL"):
        claude_auth.start_oauth_login(claude_auth.OAuthProvider.CLAUDEAI)


def test_oauth_session_raises_on_timeout_waiting_for_url(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_process = _FakePexpectProcess(url_match=None, expect_return_index=2)
    monkeypatch.setattr(
        claude_auth, "pexpect_spawner", lambda *_args, **_kwargs: fake_process
    )
    with pytest.raises(claude_auth.ClaudeAuthError, match="Timed out"):
        claude_auth.start_oauth_login(claude_auth.OAuthProvider.CLAUDEAI)


def test_submit_oauth_code_drives_subprocess_and_returns_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_url = "https://claude.ai/oauth/authorize?x=1"
    fake_process = _FakePexpectProcess(url_match=fake_url, expect_return_index=0)
    monkeypatch.setattr(
        claude_auth, "pexpect_spawner", lambda *_args, **_kwargs: fake_process
    )
    monkeypatch.setattr(
        claude_auth,
        "command_runner",
        lambda _cmd, _timeout: _FakeFinishedProcess(
            stdout='{"loggedIn": true, "email": "x@y.com"}'
        ),
    )
    start = claude_auth.start_oauth_login(claude_auth.OAuthProvider.CLAUDEAI)
    status = claude_auth.submit_oauth_code(start.session_id, "CODE#STATE")
    assert status.logged_in is True
    assert status.email == "x@y.com"
    assert fake_process.sendline_calls == ["CODE#STATE"]
