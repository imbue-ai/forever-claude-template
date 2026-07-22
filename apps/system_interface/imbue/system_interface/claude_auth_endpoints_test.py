"""Integration tests for the /api/claude-auth/* endpoints.

Each test builds a `ClaudeAuthService` and/or `WelcomeResender` with
deterministic fakes and passes them to `create_application`, which stores
them on the app's `SystemInterfaceState` for the handlers to read. This
exercises the auth-success chokepoint end-to-end through the Flask test
client without touching real Claude binaries or session transcripts -- and
without `unittest.mock` or runtime attribute patching.
"""

from __future__ import annotations

import json
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
from imbue.system_interface.testing import build_test_state
from imbue.system_interface.welcome_resend import WelcomeResender

# The initial chat agent's id, as the bootstrap would persist it.
_CHAT_AGENT_ID = "agent-00000000000000000000000000000001"

_FAKE_URL = "https://claude.com/cai/oauth/authorize?code=true&state=abc"
_FAKE_TOKEN = "sk-ant-oat01-ENDPOINT_fake-1"

_LIST_PAYLOAD = json.dumps(
    {
        "agents": [
            {"name": "ababa", "type": "claude", "state": "RUNNING"},
            {"name": "system-services", "type": "main", "state": "RUNNING"},
            {"name": "worker-1", "type": "worker", "state": "WAITING"},
        ]
    }
)


@pytest.fixture
def isolated_claude_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_dir = tmp_path / "claude-config"
    config_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.delenv("MNGR_HOST_DIR", raising=False)
    return config_dir


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
    """Build a Flask test client, injecting the auth collaborators into the app state.

    Each argument left as None gets a default production instance -- fine for
    tests that never reach that dependency (e.g. request-validation rejections).
    """
    state = build_test_state(claude_auth_service=claude_auth_service, welcome_resender=welcome_resender)
    yield create_application(state).test_client()


def _logged_in_runner(_cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
    return FakeFinishedProcess(stdout='{"loggedIn": true, "email": "u@example.com", "subscriptionType": "Max"}')


def _build_welcome_resender(host_dir: Path, welcome_calls: list[str]) -> WelcomeResender:
    _persist_chat_agent_id(host_dir)
    skill_path = host_dir / "SKILL.md"
    skill_path.write_text("---\nname: w\n---\n\nIntro\n\n---\n\n### Welcome to Minds\n\nbody\n\n---\n")

    def _record_welcome_send(agent_id: str, _message: str) -> bool:
        welcome_calls.append(agent_id)
        return True

    return WelcomeResender(
        resolve_agent=lambda _id: _fake_chat_agent(),
        read_assistant_transcript=lambda _agent: "",
        send_message_fn=_record_welcome_send,
        skill_path=skill_path,
    )


def test_status_endpoint_returns_parsed_payload(isolated_claude_config: Path) -> None:
    service = ClaudeAuthService(command_runner=_logged_in_runner)
    with _client(claude_auth_service=service) as client:
        response = client.get("/api/claude-auth/status")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["logged_in"] is True
    assert payload["email"] == "u@example.com"
    assert payload["subscription_type"] == "Max"
    assert payload["auth_mode"] == "none"


def test_status_endpoint_reports_settings_derived_mode(isolated_claude_config: Path) -> None:
    (isolated_claude_config / "settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_API_KEY": "sk-1234", "ANTHROPIC_BASE_URL": "https://litellm.example"}})
    )
    service = ClaudeAuthService(command_runner=_logged_in_runner)
    with _client(claude_auth_service=service) as client:
        response = client.get("/api/claude-auth/status")
    payload = response.get_json()
    assert payload["auth_mode"] == "imbue"
    assert payload["masked_key_suffix"] == "1234"


def test_status_endpoint_logged_out_when_claude_missing(isolated_claude_config: Path) -> None:
    def _missing_runner(_cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        raise ProcessSetupError(command=("claude",), stdout="", stderr="not found", is_output_already_logged=False)

    service = ClaudeAuthService(command_runner=_missing_runner)
    with _client(claude_auth_service=service) as client:
        response = client.get("/api/claude-auth/status")
    assert response.status_code == 200
    assert response.get_json()["logged_in"] is False


def test_setup_token_flow_via_poll_completion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The normal subscription flow: start, poll pending, poll complete.

    Completion must write the token to the settings env block, restart the
    claude-binary agents, and fire the welcome-resend chokepoint.
    """
    config_dir = tmp_path / "claude-config"
    config_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    welcome_calls: list[str] = []
    command_log: list[tuple[str, ...]] = []

    def _runner(cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        command_log.append(tuple(cmd))
        if cmd[1] == "list":
            return FakeFinishedProcess(stdout=_LIST_PAYLOAD)
        if cmd[0] == "mngr":
            return FakeFinishedProcess(returncode=0)
        return FakeFinishedProcess(stdout='{"loggedIn": true, "authMethod": "oauth_token"}')

    fake_process = FakePexpectProcess(
        [
            (0, _FAKE_URL),
            (2, ""),
            (0, f"Your OAuth token (valid for 1 year):\n{_FAKE_TOKEN}\n"),
        ]
    )
    service = ClaudeAuthService(command_runner=_runner, pexpect_spawner=lambda *_a, **_k: fake_process)
    resender = _build_welcome_resender(tmp_path, welcome_calls)

    with _client(claude_auth_service=service, welcome_resender=resender) as client:
        start = client.post("/api/claude-auth/setup-token/start")
        assert start.status_code == 200
        start_payload = start.get_json()
        assert start_payload["oauth_url"] == _FAKE_URL
        session_id = start_payload["session_id"]

        pending = client.post("/api/claude-auth/setup-token/poll", json={"session_id": session_id})
        assert pending.status_code == 200
        assert pending.get_json() == {"is_complete": False, "status": None}
        assert welcome_calls == []

        complete = client.post("/api/claude-auth/setup-token/poll", json={"session_id": session_id})
    assert complete.status_code == 200
    body = complete.get_json()
    assert body["is_complete"] is True
    assert body["status"]["logged_in"] is True
    assert body["status"]["auth_mode"] == "subscription"
    settings = json.loads((config_dir / "settings.json").read_text())
    assert settings["env"] == {"CLAUDE_CODE_OAUTH_TOKEN": _FAKE_TOKEN}
    assert welcome_calls == [_CHAT_AGENT_ID]
    stop_targets = [cmd[-1] for cmd in command_log if cmd[0] == "mngr" and cmd[1] == "stop"]
    assert stop_targets == ["ababa", "worker-1"]


def test_setup_token_submit_code_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = tmp_path / "claude-config"
    config_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    welcome_calls: list[str] = []

    def _runner(cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        if cmd[1] == "list":
            return FakeFinishedProcess(stdout=_LIST_PAYLOAD)
        if cmd[0] == "mngr":
            return FakeFinishedProcess(returncode=0)
        return FakeFinishedProcess(stdout='{"loggedIn": true, "authMethod": "oauth_token"}')

    fake_process = FakePexpectProcess([(0, _FAKE_URL), (0, f"token:\n{_FAKE_TOKEN}\n")])
    service = ClaudeAuthService(command_runner=_runner, pexpect_spawner=lambda *_a, **_k: fake_process)
    resender = _build_welcome_resender(tmp_path, welcome_calls)

    with _client(claude_auth_service=service, welcome_resender=resender) as client:
        start = client.post("/api/claude-auth/setup-token/start")
        session_id = start.get_json()["session_id"]
        submit = client.post(
            "/api/claude-auth/setup-token/submit-code",
            json={"session_id": session_id, "code": "FAKE#CODE"},
        )
    assert submit.status_code == 200
    assert submit.get_json()["auth_mode"] == "subscription"
    assert fake_process.sendline_calls == ["FAKE#CODE"]
    assert welcome_calls == [_CHAT_AGENT_ID]


def test_poll_rejects_unknown_session() -> None:
    with _client() as client:
        response = client.post("/api/claude-auth/setup-token/poll", json={"session_id": "nope"})
    assert response.status_code == 400


def test_submit_code_rejects_unknown_session() -> None:
    with _client() as client:
        response = client.post(
            "/api/claude-auth/setup-token/submit-code", json={"session_id": "nope", "code": "x"}
        )
    assert response.status_code == 400


def test_submit_credentials_writes_settings_and_restarts_claude_binary_agents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: write settings env, restart claude+worker agents, welcome-resend.

    The fake `mngr list` returns a claude agent, a worker agent, and the
    main-type services agent. The main agent must be skipped (restarting
    it would tear down supervisord), the claude agent (RUNNING) must be
    messaged to continue after the restart, and the worker (WAITING) must
    restart without a message.
    """
    config_dir = tmp_path / "claude-config"
    config_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))

    welcome_calls: list[str] = []
    mngr_calls: list[list[str]] = []

    def _runner(cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        if cmd[1] == "list":
            return FakeFinishedProcess(stdout=_LIST_PAYLOAD)
        if cmd[0] == "mngr":
            mngr_calls.append(cmd)
            return FakeFinishedProcess(returncode=0)
        return _logged_in_runner(cmd, _timeout)

    service = ClaudeAuthService(command_runner=_runner)
    resender = _build_welcome_resender(tmp_path, welcome_calls)

    with _client(claude_auth_service=service, welcome_resender=resender) as client:
        response = client.post(
            "/api/claude-auth/submit-credentials",
            json={"credentials": "ANTHROPIC_API_KEY=sk-ant-test-key"},
        )

    assert response.status_code == 200
    assert response.get_json()["logged_in"] is True
    settings = json.loads((config_dir / "settings.json").read_text())
    assert settings["env"] == {"ANTHROPIC_API_KEY": "sk-ant-test-key"}
    assert (tmp_path / "env").exists() is False, "the host env file must never be written by the auth flow"
    assert [f"{cmd[1]} {cmd[-1]}" for cmd in mngr_calls[:4]] == [
        "stop ababa",
        "stop worker-1",
        "start ababa",
        "start worker-1",
    ]
    message_calls = [cmd for cmd in mngr_calls if cmd[1] == "message"]
    assert [cmd[2] for cmd in message_calls] == ["ababa"]
    assert welcome_calls == [_CHAT_AGENT_ID]


def test_submit_credentials_rejects_unmanaged_keys(isolated_claude_config: Path) -> None:
    with _client() as client:
        response = client.post(
            "/api/claude-auth/submit-credentials",
            json={"credentials": "SOME_RANDOM_KEY=x"},
        )
    assert response.status_code == 400
    assert "Unsupported keys" in response.get_json()["detail"]


def test_submit_credentials_rejects_mixed_modes(isolated_claude_config: Path) -> None:
    with _client() as client:
        response = client.post(
            "/api/claude-auth/submit-credentials",
            json={"credentials": f"ANTHROPIC_API_KEY=sk-1\nCLAUDE_CODE_OAUTH_TOKEN={_FAKE_TOKEN}"},
        )
    assert response.status_code == 400
    assert "not both" in response.get_json()["detail"]


def test_submit_credentials_rejects_empty_body() -> None:
    with _client() as client:
        response = client.post(
            "/api/claude-auth/submit-credentials",
            json={"credentials": "   "},
        )
    assert response.status_code == 400


def test_abort_endpoint_clears_in_flight_session() -> None:
    fake_process = FakePexpectProcess([(0, _FAKE_URL)])
    service = ClaudeAuthService(pexpect_spawner=lambda *_args, **_kwargs: fake_process)

    with _client(claude_auth_service=service) as client:
        start = client.post("/api/claude-auth/setup-token/start")
        assert start.status_code == 200
        abort = client.post("/api/claude-auth/abort")
        assert abort.status_code == 200
        followup = client.post(
            "/api/claude-auth/setup-token/poll",
            json={"session_id": start.get_json()["session_id"]},
        )
    assert followup.status_code == 400
