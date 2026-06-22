"""Integration tests for the /api/claude-auth/* endpoints.

Each test builds a `ClaudeAuthService` and/or `WelcomeResender` with
deterministic fakes and passes them to `create_application`, which stores
them on the app's `SystemInterfaceState` for the handlers to read. This
exercises the auth-success chokepoint end-to-end through the Flask test
client without touching real Claude binaries or session transcripts -- and
without `unittest.mock` or runtime attribute patching.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from imbue.system_interface import welcome_resend
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.claude_auth import ClaudeAuthService
from imbue.system_interface.claude_auth import ProcessSetupError
from imbue.system_interface.server import create_application
from imbue.system_interface.testing import FakeFinishedProcess
from imbue.system_interface.testing import FakePexpectProcess
from imbue.system_interface.welcome_resend import WelcomeResender

# The initial chat agent's id, as the bootstrap would persist it.
_CHAT_AGENT_ID = "agent-00000000000000000000000000000001"


def _fake_chat_agent() -> AgentInfo:
    """A resolved initial-chat-agent AgentInfo (valid id) for welcome-resend tests."""
    return AgentInfo(
        id=_CHAT_AGENT_ID,
        name="chat",
        state="RUNNING",
        agent_state_dir=Path("/tmp/agent"),
        claude_config_dir=Path("/tmp/.claude"),
    )


def _persist_chat_agent_id(host_dir: Path) -> None:
    """Write the initial chat agent's id where welcome_resend reads it back."""
    (host_dir / welcome_resend._INITIAL_CHAT_AGENT_ID_FILENAME).write_text(_CHAT_AGENT_ID)


@contextmanager
def _client(
    claude_auth_service: ClaudeAuthService | None = None,
    welcome_resender: WelcomeResender | None = None,
) -> Iterator[FlaskClient]:
    """Build a Flask test client over an app wired with the given service instances.

    Either argument left as None falls back to a default production
    instance -- fine for tests that never reach that dependency (e.g.
    request-validation rejections).
    """
    app = create_application(claude_auth_service=claude_auth_service, welcome_resender=welcome_resender)
    yield app.test_client()


def _logged_in_runner(_cmd: list[str], _timeout: float) -> FakeFinishedProcess:
    return FakeFinishedProcess(stdout='{"loggedIn": true, "email": "u@example.com", "subscriptionType": "Max"}')


def test_status_endpoint_returns_parsed_payload() -> None:
    service = ClaudeAuthService(command_runner=_logged_in_runner)
    with _client(claude_auth_service=service) as client:
        response = client.get("/api/claude-auth/status")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["logged_in"] is True
    assert payload["email"] == "u@example.com"
    assert payload["subscription_type"] == "Max"


def test_status_endpoint_logged_out_when_claude_missing() -> None:
    def _missing_runner(_cmd: list[str], _timeout: float) -> FakeFinishedProcess:
        raise ProcessSetupError(command=("claude",), stdout="", stderr="not found", is_output_already_logged=False)

    service = ClaudeAuthService(command_runner=_missing_runner)
    with _client(claude_auth_service=service) as client:
        response = client.get("/api/claude-auth/status")
    assert response.status_code == 200
    assert response.get_json()["logged_in"] is False


def test_start_oauth_rejects_unknown_provider() -> None:
    with _client() as client:
        response = client.post("/api/claude-auth/start", json={"provider": "bogus"})
    assert response.status_code == 400


def test_full_oauth_flow_drives_subprocess_runs_welcome_resend_and_skips_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Subscription OAuth (`--claudeai`) needs no agent restart.

    The asserted absence of `mngr stop`/`mngr start` calls is the
    behavioral contract: the subscription credential is re-read live on
    the next API call, so restart would be disruptive churn for no auth
    benefit. (The console provider differs -- see the console restart
    test in claude_auth_test.py.) The welcome-resend target is the
    initial chat agent, resolved from the id the bootstrap persisted.
    """
    fake_url = "https://claude.ai/oauth/authorize?abc=1"
    fake_process = FakePexpectProcess(url_match=fake_url)
    welcome_resend_calls: list[str] = []
    command_log: list[tuple[str, ...]] = []

    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    _persist_chat_agent_id(tmp_path)
    skill_path = tmp_path / "SKILL.md"
    skill_path.write_text("---\nname: w\n---\n\nIntro\n\n---\n\n### Welcome to Minds\n\nbody\n\n---\n")

    def _recording_runner(cmd: list[str], timeout: float) -> FakeFinishedProcess:
        command_log.append(tuple(cmd))
        return _logged_in_runner(cmd, timeout)

    def _record_welcome_send(agent_id: str, _message: str) -> bool:
        welcome_resend_calls.append(agent_id)
        return True

    service = ClaudeAuthService(command_runner=_recording_runner, pexpect_spawner=lambda *_a, **_k: fake_process)
    resender = WelcomeResender(
        resolve_agent=lambda _id: _fake_chat_agent(),
        read_assistant_transcript=lambda _agent: "",
        send_message_fn=_record_welcome_send,
        skill_path=skill_path,
    )

    with _client(claude_auth_service=service, welcome_resender=resender) as client:
        start = client.post("/api/claude-auth/start", json={"provider": "claudeai"})
        assert start.status_code == 200
        start_payload = start.get_json()
        assert start_payload["oauth_url"] == fake_url
        session_id = start_payload["session_id"]

        submit = client.post(
            "/api/claude-auth/submit-code",
            json={"session_id": session_id, "code": "FAKE#CODE"},
        )
    assert submit.status_code == 200
    body = submit.get_json()
    assert body["logged_in"] is True
    assert body["email"] == "u@example.com"
    assert fake_process.sendline_calls == ["FAKE#CODE"]
    assert welcome_resend_calls == [_CHAT_AGENT_ID]
    assert all(cmd[:2] != ("mngr", "stop") for cmd in command_log)
    assert all(cmd[:2] != ("mngr", "start") for cmd in command_log)


def test_submit_code_rejects_unknown_session() -> None:
    with _client() as client:
        response = client.post("/api/claude-auth/submit-code", json={"session_id": "nope", "code": "x"})
    assert response.status_code == 400


def test_submit_api_key_restarts_all_claude_agents_and_runs_welcome_resend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: write env, restart every type:claude agent, welcome-resend.

    The fake `mngr list` returns three agents: two `type: claude` and one
    `type: main`. We assert the main-type agent is skipped (matches
    system-services' shape in a real mind) and both claude agents are
    stopped before either is restarted. The welcome-resend target is the
    initial chat agent, resolved from the id the bootstrap persisted.
    """
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    _persist_chat_agent_id(tmp_path)
    skill_path = tmp_path / "SKILL.md"
    skill_path.write_text("---\nname: w\n---\n\nIntro\n\n---\n\n### Welcome to Minds\n\nbody\n\n---\n")

    welcome_calls: list[str] = []
    restart_calls: list[list[str]] = []
    list_payload = (
        '{"agents": ['
        '{"name": "ababa", "type": "claude"}, '
        '{"name": "system-services", "type": "main"}, '
        '{"name": "worktree-1", "type": "claude"}'
        "]}"
    )

    def _runner(cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        if cmd[:3] == ["mngr", "list", "--format"]:
            return FakeFinishedProcess(stdout=list_payload)
        if len(cmd) >= 3 and cmd[0] == "mngr" and cmd[1] in {"stop", "start"}:
            restart_calls.append(cmd)
            return FakeFinishedProcess(returncode=0)
        return _logged_in_runner(cmd, _timeout)

    def _record_welcome_send(agent_id: str, _message: str) -> bool:
        welcome_calls.append(agent_id)
        return True

    service = ClaudeAuthService(command_runner=_runner)
    resender = WelcomeResender(
        resolve_agent=lambda _id: _fake_chat_agent(),
        read_assistant_transcript=lambda _agent: "",
        send_message_fn=_record_welcome_send,
        skill_path=skill_path,
    )

    with _client(claude_auth_service=service, welcome_resender=resender) as client:
        response = client.post(
            "/api/claude-auth/submit-api-key",
            json={"api_key": "sk-ant-test-key"},
        )

    assert response.status_code == 200
    assert response.get_json()["logged_in"] is True
    env_text = (tmp_path / "env").read_text()
    assert "ANTHROPIC_API_KEY=sk-ant-test-key" in env_text
    assert [f"{cmd[1]} {cmd[-1]}" for cmd in restart_calls] == [
        "stop ababa",
        "stop worktree-1",
        "start ababa",
        "start worktree-1",
    ]
    assert all("--no-resume" in cmd for cmd in restart_calls if cmd[1] == "start")
    assert welcome_calls == [_CHAT_AGENT_ID]


def test_submit_api_key_rejects_empty_key() -> None:
    with _client() as client:
        response = client.post(
            "/api/claude-auth/submit-api-key",
            json={"api_key": "   "},
        )
    assert response.status_code == 400


def test_abort_endpoint_clears_in_flight_session() -> None:
    fake_url = "https://claude.ai/oauth/authorize?x=1"
    fake_process = FakePexpectProcess(url_match=fake_url)
    service = ClaudeAuthService(pexpect_spawner=lambda *_args, **_kwargs: fake_process)

    with _client(claude_auth_service=service) as client:
        start = client.post("/api/claude-auth/start", json={"provider": "claudeai"})
        assert start.status_code == 200
        abort = client.post("/api/claude-auth/abort")
        assert abort.status_code == 200
        followup = client.post(
            "/api/claude-auth/submit-code",
            json={"session_id": start.get_json()["session_id"], "code": "x"},
        )
    assert followup.status_code == 400
