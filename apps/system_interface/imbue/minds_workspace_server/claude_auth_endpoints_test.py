"""Integration tests for the /api/claude-auth/* endpoints.

Tests use `monkeypatch.setattr` to swap the injectable module-level
callables (`command_runner`, `pexpect_spawner`, `capture_pane`,
`send_message_fn`) so the auth-success chokepoint is exercised end-to-end
through the FastAPI test client without touching real Claude binaries
or tmux sessions.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from imbue.minds_workspace_server import claude_auth
from imbue.minds_workspace_server import welcome_resend
from imbue.minds_workspace_server.server import create_application


class _FakeFinishedProcess:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class _FakePexpectProcess:
    def __init__(self, url: str | None) -> None:
        self._url = url
        self._call_count = 0
        self.sendline_calls: list[str] = []
        self.timeout: float | None = None
        self.match: Any = None
        if url is not None:
            self.match = re.compile(r".*").match(url)
            assert self.match is not None

    def expect(self, _patterns: object) -> int:
        self._call_count += 1
        if self._call_count == 1:
            return 0 if self._url is not None else 1
        return 0

    def sendline(self, s: str) -> None:
        self.sendline_calls.append(s)

    def isalive(self) -> bool:
        return True

    def terminate(self, force: bool = False) -> None:
        pass

    def close(self) -> None:
        pass


@pytest.fixture
def app() -> FastAPI:
    return create_application()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def reset_oauth_session() -> Iterator[None]:
    claude_auth.abort_oauth_login()
    yield
    claude_auth.abort_oauth_login()


def _logged_in_runner(_cmd: list[str], _timeout: float) -> _FakeFinishedProcess:
    return _FakeFinishedProcess(
        stdout='{"loggedIn": true, "email": "u@example.com", "subscriptionType": "Max"}'
    )


def test_status_endpoint_returns_parsed_payload(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_auth, "command_runner", _logged_in_runner)
    response = client.get("/api/claude-auth/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["logged_in"] is True
    assert payload["email"] == "u@example.com"
    assert payload["subscription_type"] == "Max"


def test_status_endpoint_logged_out_when_claude_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _missing_runner(_cmd: list[str], _timeout: float) -> _FakeFinishedProcess:
        raise claude_auth.ProcessSetupError(
            command=("claude",), stdout="", stderr="not found", is_output_already_logged=False
        )

    monkeypatch.setattr(claude_auth, "command_runner", _missing_runner)
    response = client.get("/api/claude-auth/status")
    assert response.status_code == 200
    assert response.json()["logged_in"] is False


def test_start_oauth_rejects_unknown_provider(client: TestClient) -> None:
    response = client.post("/api/claude-auth/start", json={"provider": "bogus"})
    assert response.status_code == 400


def test_full_oauth_flow_drives_subprocess_runs_welcome_resend_and_skips_restart(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OAuth path writes `.credentials.json` via the PTY; no agent restart needed.

    The asserted absence of `mngr stop`/`mngr start` calls is the
    behavioral contract: claude code auto-picks up freshly-written
    credentials on its next API call, so restart would be disruptive
    churn for no auth benefit.
    """
    fake_url = "https://claude.ai/oauth/authorize?abc=1"
    fake_process = _FakePexpectProcess(url=fake_url)
    welcome_resend_calls: list[str] = []
    command_log: list[tuple[str, ...]] = []

    skill_path = tmp_path / "SKILL.md"
    skill_path.write_text(
        "---\nname: w\n---\n\nIntro\n\n---\n\n### Welcome to Minds\n\nbody\n\n---\n"
    )

    def _recording_runner(cmd: list[str], timeout: float) -> _FakeFinishedProcess:
        command_log.append(tuple(cmd))
        return _logged_in_runner(cmd, timeout)

    monkeypatch.setattr(
        claude_auth, "pexpect_spawner", lambda *_args, **_kwargs: fake_process
    )
    monkeypatch.setattr(claude_auth, "command_runner", _recording_runner)
    monkeypatch.setattr(welcome_resend, "capture_pane", lambda _name: "empty pane")
    monkeypatch.setattr(
        welcome_resend,
        "send_message_fn",
        lambda name, _message: (welcome_resend_calls.append(name), True)[1],
    )
    monkeypatch.setattr(welcome_resend, "_DEFAULT_SKILL_PATH", skill_path)

    start = client.post("/api/claude-auth/start", json={"provider": "claudeai"})
    assert start.status_code == 200
    start_payload = start.json()
    assert start_payload["oauth_url"] == fake_url
    session_id = start_payload["session_id"]

    submit = client.post(
        "/api/claude-auth/submit-code",
        json={"session_id": session_id, "code": "FAKE#CODE", "chat_agent_name": "chat-1"},
    )
    assert submit.status_code == 200
    body = submit.json()
    assert body["logged_in"] is True
    assert body["email"] == "u@example.com"
    assert fake_process.sendline_calls == ["FAKE#CODE"]
    assert welcome_resend_calls == ["chat-1"]
    assert all(cmd[:2] != ("mngr", "stop") for cmd in command_log)
    assert all(cmd[:2] != ("mngr", "start") for cmd in command_log)


def test_submit_code_rejects_unknown_session(client: TestClient) -> None:
    response = client.post(
        "/api/claude-auth/submit-code", json={"session_id": "nope", "code": "x"}
    )
    assert response.status_code == 400


def test_submit_api_key_restarts_all_claude_agents_and_runs_welcome_resend(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: write env, restart every type:claude agent, welcome-resend.

    The fake `mngr list` returns three agents: two `type: claude` and one
    `type: main`. We assert the main-type agent is skipped (matches
    system-services' shape in a real mind) and both claude agents are
    restarted via the same `mngr stop`/`mngr start` pair.
    """
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    skill_path = tmp_path / "SKILL.md"
    skill_path.write_text(
        "---\nname: w\n---\n\nIntro\n\n---\n\n### Welcome to Minds\n\nbody\n\n---\n"
    )
    monkeypatch.setattr(welcome_resend, "_DEFAULT_SKILL_PATH", skill_path)

    welcome_calls: list[str] = []
    restart_calls: list[str] = []
    list_payload = (
        '{"agents": ['
        '{"name": "ababa", "type": "claude"}, '
        '{"name": "system-services", "type": "main"}, '
        '{"name": "worktree-1", "type": "claude"}'
        "]}"
    )

    def _runner(cmd: list[str], _timeout: float) -> _FakeFinishedProcess:
        if cmd[:3] == ["mngr", "list", "--format"]:
            return _FakeFinishedProcess(stdout=list_payload)
        if len(cmd) >= 3 and cmd[0] == "mngr" and cmd[1] in {"stop", "start"}:
            restart_calls.append(f"{cmd[1]} {cmd[2]}")
            return _FakeFinishedProcess(returncode=0)
        return _logged_in_runner(cmd, _timeout)

    monkeypatch.setattr(claude_auth, "command_runner", _runner)
    monkeypatch.setattr(welcome_resend, "capture_pane", lambda _name: "empty")
    monkeypatch.setattr(
        welcome_resend,
        "send_message_fn",
        lambda name, _message: (welcome_calls.append(name), True)[1],
    )

    response = client.post(
        "/api/claude-auth/submit-api-key",
        json={"api_key": "sk-ant-test-key", "chat_agent_name": "ababa"},
    )

    assert response.status_code == 200
    assert response.json()["logged_in"] is True
    env_text = (tmp_path / "env").read_text()
    assert "ANTHROPIC_API_KEY=sk-ant-test-key" in env_text
    assert restart_calls == [
        "stop ababa",
        "start ababa",
        "stop worktree-1",
        "start worktree-1",
    ]
    assert welcome_calls == ["ababa"]


def test_submit_api_key_rejects_empty_key(client: TestClient) -> None:
    response = client.post(
        "/api/claude-auth/submit-api-key",
        json={"api_key": "   ", "chat_agent_name": "chat-1"},
    )
    assert response.status_code == 400


def test_abort_endpoint_clears_in_flight_session(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_url = "https://claude.ai/oauth/authorize?x=1"
    fake_process = _FakePexpectProcess(url=fake_url)
    monkeypatch.setattr(
        claude_auth, "pexpect_spawner", lambda *_args, **_kwargs: fake_process
    )

    start = client.post("/api/claude-auth/start", json={"provider": "claudeai"})
    assert start.status_code == 200
    abort = client.post("/api/claude-auth/abort")
    assert abort.status_code == 200
    followup = client.post(
        "/api/claude-auth/submit-code",
        json={"session_id": start.json()["session_id"], "code": "x"},
    )
    assert followup.status_code == 400
