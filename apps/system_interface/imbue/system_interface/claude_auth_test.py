"""Tests for the claude_auth backend module.

`ClaudeAuthService` takes its outside-world dependencies (`command_runner`,
`pexpect_spawner`) as constructor arguments, so each test builds an
isolated instance with deterministic fakes -- no `unittest.mock`, and no
runtime patching of module attributes. The pure module-level helpers
(`_parse_status_payload`, `parse_credential_lines`, `derive_auth_mode`,
the settings-env reader/writer, the URL/token extraction) are tested
directly. `CLAUDE_CONFIG_DIR` is pointed at a tmp dir via
`monkeypatch.setenv` (environment adjustment, not object patching) so no
test reads the developer's real shared Claude settings.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mngr_cli_contract.contract import assert_mngr_argv_valid
from pydantic import SecretStr

from imbue.mngr.cli.exit_codes import EXIT_CODE_PROVIDER_INACCESSIBLE
from imbue.system_interface import claude_auth
from imbue.system_interface.testing import FakeFinishedProcess
from imbue.system_interface.testing import FakePexpectProcess

_FAKE_URL = "https://claude.com/cai/oauth/authorize?code=true&state=abc"
_FAKE_TOKEN = "sk-ant-oat01-FAKE_token-123"


@pytest.fixture
def isolated_claude_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point CLAUDE_CONFIG_DIR at a tmp dir so tests never touch real settings."""
    config_dir = tmp_path / "claude-config"
    config_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.delenv("MNGR_HOST_DIR", raising=False)
    return config_dir


# ----- status parsing -----


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


def test_get_auth_status_returns_logged_out_when_runner_raises(isolated_claude_config: Path) -> None:
    def _runner(_cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        raise claude_auth.ProcessSetupError(
            command=("claude",), stdout="", stderr="not found", is_output_already_logged=False
        )

    service = claude_auth.ClaudeAuthService(command_runner=_runner)
    status = service.get_auth_status()
    assert status.logged_in is False
    assert status.auth_mode is claude_auth.AuthMode.NONE


def test_get_auth_status_parses_logged_in_json(isolated_claude_config: Path) -> None:
    def _runner(_cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        return FakeFinishedProcess(stdout='{"loggedIn": true, "email": "x@y.com", "subscriptionType": "Pro"}')

    service = claude_auth.ClaudeAuthService(command_runner=_runner)
    status = service.get_auth_status()
    assert status.logged_in is True
    assert status.email == "x@y.com"
    assert status.subscription_type == "Pro"


def test_get_auth_status_rejects_non_json_output(isolated_claude_config: Path) -> None:
    def _runner(_cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        return FakeFinishedProcess(stdout="not json at all")

    service = claude_auth.ClaudeAuthService(command_runner=_runner)
    with pytest.raises(claude_auth.ClaudeAuthError, match="non-JSON"):
        service.get_auth_status()


def test_get_auth_status_treats_empty_output_as_logged_out(isolated_claude_config: Path) -> None:
    def _runner(_cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        return FakeFinishedProcess(stdout="")

    service = claude_auth.ClaudeAuthService(command_runner=_runner)
    status = service.get_auth_status()
    assert status.logged_in is False


def test_get_auth_status_overlays_managed_settings_env_onto_subprocess(isolated_claude_config: Path) -> None:
    """The status subprocess must see the settings-managed credentials.

    The long-lived system-interface process never receives settings-env
    values in its own environment, so `get_auth_status` overlays whatever
    is currently in the managed env block -- otherwise a freshly written
    key would misreport as logged out.
    """
    (isolated_claude_config / "settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_API_KEY": "sk-ant-managed-key"}})
    )
    seen_envs: list[dict[str, str] | None] = []

    def _runner(_cmd: list[str], _timeout: float, env: dict[str, str] | None = None) -> FakeFinishedProcess:
        seen_envs.append(env)
        return FakeFinishedProcess(stdout='{"loggedIn": true, "authMethod": "api_key"}')

    service = claude_auth.ClaudeAuthService(command_runner=_runner)
    status = service.get_auth_status()
    assert status.auth_mode is claude_auth.AuthMode.API_KEY
    assert status.masked_key_suffix == "-key"
    assert seen_envs and seen_envs[0] is not None
    assert seen_envs[0]["ANTHROPIC_API_KEY"] == "sk-ant-managed-key"


# ----- credential-lines parsing -----


def test_parse_credential_lines_accepts_api_key_alone() -> None:
    parsed = claude_auth.parse_credential_lines("ANTHROPIC_API_KEY=sk-ant-abc")
    assert parsed == {"ANTHROPIC_API_KEY": "sk-ant-abc"}


def test_parse_credential_lines_accepts_key_with_base_url() -> None:
    parsed = claude_auth.parse_credential_lines(
        "ANTHROPIC_BASE_URL=https://litellm.example.com\nANTHROPIC_API_KEY=sk-litellm-1"
    )
    assert parsed == {
        "ANTHROPIC_BASE_URL": "https://litellm.example.com",
        "ANTHROPIC_API_KEY": "sk-litellm-1",
    }


def test_parse_credential_lines_accepts_oauth_token_alone() -> None:
    parsed = claude_auth.parse_credential_lines(f"CLAUDE_CODE_OAUTH_TOKEN={_FAKE_TOKEN}")
    assert parsed == {"CLAUDE_CODE_OAUTH_TOKEN": _FAKE_TOKEN}


def test_parse_credential_lines_rejects_unknown_keys() -> None:
    with pytest.raises(claude_auth.CredentialPasteError, match="Unsupported keys.*SOME_OTHER_KEY"):
        claude_auth.parse_credential_lines("ANTHROPIC_API_KEY=sk-1\nSOME_OTHER_KEY=x")


def test_parse_credential_lines_rejects_mixed_token_and_key() -> None:
    with pytest.raises(claude_auth.CredentialPasteError, match="not both"):
        claude_auth.parse_credential_lines(f"ANTHROPIC_API_KEY=sk-1\nCLAUDE_CODE_OAUTH_TOKEN={_FAKE_TOKEN}")


def test_parse_credential_lines_rejects_base_url_without_key() -> None:
    with pytest.raises(claude_auth.CredentialPasteError, match="requires an accompanying"):
        claude_auth.parse_credential_lines("ANTHROPIC_BASE_URL=https://litellm.example.com")


def test_parse_credential_lines_rejects_empty_paste() -> None:
    with pytest.raises(claude_auth.CredentialPasteError, match="No credentials found"):
        claude_auth.parse_credential_lines("   \n# just a comment\n")


def test_parse_credential_lines_strips_quotes_and_whitespace() -> None:
    parsed = claude_auth.parse_credential_lines('ANTHROPIC_API_KEY="sk-ant-quoted"  ')
    assert parsed == {"ANTHROPIC_API_KEY": "sk-ant-quoted"}


# ----- mode derivation -----


def test_derive_auth_mode_covers_all_shapes() -> None:
    assert claude_auth.derive_auth_mode({}) is claude_auth.AuthMode.NONE
    assert claude_auth.derive_auth_mode({"ANTHROPIC_API_KEY": "k"}) is claude_auth.AuthMode.API_KEY
    assert (
        claude_auth.derive_auth_mode({"ANTHROPIC_API_KEY": "k", "ANTHROPIC_BASE_URL": "u"})
        is claude_auth.AuthMode.IMBUE
    )
    assert claude_auth.derive_auth_mode({"CLAUDE_CODE_OAUTH_TOKEN": "t"}) is claude_auth.AuthMode.SUBSCRIPTION


def test_derive_auth_mode_key_outranks_token() -> None:
    """Mirrors Claude Code's own precedence: a key present wins over a token."""
    managed = {"ANTHROPIC_API_KEY": "k", "CLAUDE_CODE_OAUTH_TOKEN": "t"}
    assert claude_auth.derive_auth_mode(managed) is claude_auth.AuthMode.API_KEY


def test_masked_credential_suffix_prefers_key_and_handles_absence() -> None:
    assert claude_auth.masked_credential_suffix({}) is None
    assert claude_auth.masked_credential_suffix({"ANTHROPIC_API_KEY": "sk-ant-abcd"}) == "abcd"
    assert claude_auth.masked_credential_suffix({"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-wxyz"}) == "wxyz"


# ----- settings-env reader/writer -----


def test_write_managed_auth_env_creates_settings_file(isolated_claude_config: Path) -> None:
    claude_auth.write_managed_auth_env({"ANTHROPIC_API_KEY": "sk-1"})
    settings = json.loads((isolated_claude_config / "settings.json").read_text())
    assert settings["env"] == {"ANTHROPIC_API_KEY": "sk-1"}


def test_write_managed_auth_env_preserves_other_settings_and_env_keys(isolated_claude_config: Path) -> None:
    settings_path = isolated_claude_config / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "model": "opus[1m]",
                "env": {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "64000", "ANTHROPIC_API_KEY": "sk-old"},
            }
        )
    )
    claude_auth.write_managed_auth_env({"CLAUDE_CODE_OAUTH_TOKEN": _FAKE_TOKEN})
    settings = json.loads(settings_path.read_text())
    assert settings["model"] == "opus[1m]"
    assert settings["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "64000"
    assert settings["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == _FAKE_TOKEN
    # Fully controlled: the previous mode's key was deleted, not left to
    # shadow the freshly written token (a key outranks a token at runtime).
    assert "ANTHROPIC_API_KEY" not in settings["env"]


def test_write_managed_auth_env_mode_switch_deletes_base_url(isolated_claude_config: Path) -> None:
    claude_auth.write_managed_auth_env({"ANTHROPIC_API_KEY": "sk-1", "ANTHROPIC_BASE_URL": "https://x"})
    claude_auth.write_managed_auth_env({"ANTHROPIC_API_KEY": "sk-2"})
    settings = json.loads((isolated_claude_config / "settings.json").read_text())
    assert settings["env"] == {"ANTHROPIC_API_KEY": "sk-2"}


def test_write_managed_auth_env_refuses_unmanaged_keys(isolated_claude_config: Path) -> None:
    with pytest.raises(claude_auth.ClaudeAuthError, match="unmanaged"):
        claude_auth.write_managed_auth_env({"SOME_KEY": "x"})


def test_write_managed_auth_env_raises_on_corrupt_settings(isolated_claude_config: Path) -> None:
    (isolated_claude_config / "settings.json").write_text("{not json")
    with pytest.raises(claude_auth.ClaudeAuthError, match="corrupt"):
        claude_auth.write_managed_auth_env({"ANTHROPIC_API_KEY": "sk-1"})


def test_read_managed_auth_env_returns_only_managed_keys(isolated_claude_config: Path) -> None:
    (isolated_claude_config / "settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_API_KEY": "sk-1", "OTHER": "x"}})
    )
    assert claude_auth.read_managed_auth_env() == {"ANTHROPIC_API_KEY": "sk-1"}


def test_read_managed_auth_env_tolerates_missing_and_corrupt_files(isolated_claude_config: Path) -> None:
    assert claude_auth.read_managed_auth_env() == {}
    (isolated_claude_config / "settings.json").write_text("{broken")
    assert claude_auth.read_managed_auth_env() == {}


# ----- workspace host id -----


def test_read_workspace_host_id_from_data_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    (tmp_path / "data.json").write_text(json.dumps({"host_id": "host-123"}))
    assert claude_auth.read_workspace_host_id() == "host-123"


def test_read_workspace_host_id_tolerates_missing_env_and_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MNGR_HOST_DIR", raising=False)
    assert claude_auth.read_workspace_host_id() is None
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    assert claude_auth.read_workspace_host_id() is None


# ----- agent snapshot / restart -----

_LIST_PAYLOAD = json.dumps(
    {
        "agents": [
            {"name": "chat-1", "type": "claude", "state": "RUNNING"},
            {"name": "system-services", "type": "main", "state": "RUNNING"},
            {"name": "worker-1", "type": "worker", "state": "RUNNING"},
            {"name": "chat-2", "type": "claude", "state": "WAITING"},
            {"name": "old-chat", "type": "claude", "state": "STOPPED"},
        ]
    }
)


def test_snapshot_includes_claude_and_worker_types_excludes_main(isolated_claude_config: Path) -> None:
    def _runner(_cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        return FakeFinishedProcess(stdout=_LIST_PAYLOAD)

    service = claude_auth.ClaudeAuthService(command_runner=_runner)
    snapshots = service.snapshot_claude_binary_agents()
    assert [(s.name, s.state) for s in snapshots] == [
        ("chat-1", "RUNNING"),
        ("worker-1", "RUNNING"),
        ("chat-2", "WAITING"),
        ("old-chat", "STOPPED"),
    ]


def test_snapshot_raises_on_mngr_failure(isolated_claude_config: Path) -> None:
    def _runner(_cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        return FakeFinishedProcess(returncode=3, stderr="boom")

    service = claude_auth.ClaudeAuthService(command_runner=_runner)
    with pytest.raises(claude_auth.ClaudeAuthError, match="mngr list failed"):
        service.snapshot_claude_binary_agents()


def test_snapshot_tolerates_provider_inaccessible_exit(isolated_claude_config: Path) -> None:
    def _runner(_cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        return FakeFinishedProcess(stdout=_LIST_PAYLOAD, returncode=EXIT_CODE_PROVIDER_INACCESSIBLE)

    service = claude_auth.ClaudeAuthService(command_runner=_runner)
    assert len(service.snapshot_claude_binary_agents()) == 4


def _build_restart_recording_service(
    command_log: list[tuple[str, ...]],
) -> claude_auth.ClaudeAuthService:
    def _runner(cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        command_log.append(tuple(cmd))
        if cmd[1] == "list":
            return FakeFinishedProcess(stdout=_LIST_PAYLOAD)
        return FakeFinishedProcess(returncode=0, stdout='{"loggedIn": true}')

    return claude_auth.ClaudeAuthService(command_runner=_runner)


def test_restart_stops_live_agents_then_starts_then_messages_previously_running(
    isolated_claude_config: Path,
) -> None:
    """The full restart contract in one pass.

    STOPPED agents are untouched; RUNNING and WAITING agents are stopped
    (all of them) before any is started; only the previously-RUNNING ones
    are messaged afterwards, with the auth-aware continue message.
    """
    command_log: list[tuple[str, ...]] = []
    service = _build_restart_recording_service(command_log)
    restarted = service.restart_all_claude_agents()
    assert restarted == ["chat-1", "worker-1", "chat-2"]
    mngr_calls = [cmd for cmd in command_log if cmd[0] == "mngr" and cmd[1] != "list"]
    assert [cmd[:2] + (cmd[-1],) for cmd in mngr_calls[:3]] == [
        ("mngr", "stop", "chat-1"),
        ("mngr", "stop", "worker-1"),
        ("mngr", "stop", "chat-2"),
    ]
    assert [cmd[:3] + (cmd[3],) for cmd in mngr_calls[3:6]] == [
        ("mngr", "start", "--no-resume", "chat-1"),
        ("mngr", "start", "--no-resume", "worker-1"),
        ("mngr", "start", "--no-resume", "chat-2"),
    ]
    message_calls = mngr_calls[6:]
    assert [cmd[2] for cmd in message_calls] == ["chat-1", "worker-1"]
    assert all(cmd[1] == "message" for cmd in message_calls)
    assert all(claude_auth.RESTART_CONTINUE_MESSAGE in cmd for cmd in message_calls)
    # "old-chat" (STOPPED) must appear in no stop/start/message call.
    assert all("old-chat" not in cmd for cmd in mngr_calls)


def test_restart_prepares_config_between_stop_and_start(isolated_claude_config: Path) -> None:
    """The shared .claude.json prep must land while every agent is stopped."""
    events: list[str] = []
    config_path = isolated_claude_config / ".claude.json"

    def _runner(cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        if cmd[1] == "list":
            return FakeFinishedProcess(stdout=_LIST_PAYLOAD)
        if cmd[1] in ("stop", "start"):
            events.append(f"{cmd[1]}:{'prepped' if config_path.exists() else 'unprepped'}")
        return FakeFinishedProcess(returncode=0, stdout='{"loggedIn": true}')

    service = claude_auth.ClaudeAuthService(command_runner=_runner)
    service.restart_all_claude_agents()
    stop_events = [event for event in events if event.startswith("stop")]
    start_events = [event for event in events if event.startswith("start")]
    assert all(event == "stop:unprepped" for event in stop_events)
    assert all(event == "start:prepped" for event in start_events)


def test_restart_tolerates_message_delivery_failure(isolated_claude_config: Path) -> None:
    """A continue-message failure must not fail the (already successful) auth flow."""

    def _runner(cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        if cmd[1] == "list":
            return FakeFinishedProcess(stdout=_LIST_PAYLOAD)
        if cmd[1] == "message":
            return FakeFinishedProcess(returncode=1, stderr="delivery failed")
        return FakeFinishedProcess(returncode=0)

    service = claude_auth.ClaudeAuthService(command_runner=_runner)
    restarted = service.restart_all_claude_agents()
    assert restarted == ["chat-1", "worker-1", "chat-2"]


def test_restart_raises_when_stop_fails(isolated_claude_config: Path) -> None:
    def _runner(cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        if cmd[1] == "list":
            return FakeFinishedProcess(stdout=_LIST_PAYLOAD)
        if cmd[1] == "stop":
            return FakeFinishedProcess(returncode=1, stderr="stop broke")
        return FakeFinishedProcess(returncode=0)

    service = claude_auth.ClaudeAuthService(command_runner=_runner)
    with pytest.raises(claude_auth.ClaudeAuthError, match="mngr stop"):
        service.restart_all_claude_agents()


def test_approve_api_key_in_claude_config_appends_suffix(isolated_claude_config: Path) -> None:
    config_path = isolated_claude_config / ".claude.json"
    config_path.write_text(json.dumps({"customApiKeyResponses": {"approved": ["oldsuffix"]}}))
    claude_auth._approve_api_key_in_claude_config(config_path, SecretStr("sk-ant-api03-" + "x" * 30))
    config = json.loads(config_path.read_text())
    assert "oldsuffix" in config["customApiKeyResponses"]["approved"]
    assert ("x" * 20) in config["customApiKeyResponses"]["approved"]


# ----- submit_credentials -----


def test_submit_credentials_writes_settings_and_restarts(isolated_claude_config: Path) -> None:
    command_log: list[tuple[str, ...]] = []
    service = _build_restart_recording_service(command_log)
    status = service.submit_credentials("ANTHROPIC_API_KEY=sk-ant-fresh")
    settings = json.loads((isolated_claude_config / "settings.json").read_text())
    assert settings["env"] == {"ANTHROPIC_API_KEY": "sk-ant-fresh"}
    assert status.auth_mode is claude_auth.AuthMode.API_KEY
    assert any(cmd[1] == "stop" for cmd in command_log)
    assert any(cmd[1] == "start" for cmd in command_log)
    # The freshly written key was pre-approved in .claude.json so restarted
    # agents don't block on the custom-key challenge.
    config = json.loads((isolated_claude_config / ".claude.json").read_text())
    assert "sk-ant-fresh"[-20:] in config["customApiKeyResponses"]["approved"]


def test_submit_credentials_rejects_bad_paste_without_touching_anything(isolated_claude_config: Path) -> None:
    command_log: list[tuple[str, ...]] = []
    service = _build_restart_recording_service(command_log)
    with pytest.raises(claude_auth.CredentialPasteError):
        service.submit_credentials("NOT_A_MANAGED_KEY=x")
    assert not (isolated_claude_config / "settings.json").exists()
    assert command_log == []


# ----- setup-token flow -----


def test_start_setup_token_extracts_url() -> None:
    fake_process = FakePexpectProcess([(0, _FAKE_URL), (2, "")])
    service = claude_auth.ClaudeAuthService(pexpect_spawner=lambda *_a, **_k: fake_process)
    result = service.start_setup_token()
    assert result.oauth_url == _FAKE_URL
    assert result.session_id


def test_start_setup_token_extracts_clean_url_from_osc8_hyperlink_stream() -> None:
    """The CLI renders the URL as an escape-wrapped OSC 8 hyperlink (doubled)."""
    wrapped = f"\x1b]8;;{_FAKE_URL}\x1b\\\x1b[94m{_FAKE_URL}\x1b[39m\x1b]8;;\x1b\\"
    fake_process = FakePexpectProcess([(0, wrapped)])
    service = claude_auth.ClaudeAuthService(pexpect_spawner=lambda *_a, **_k: fake_process)
    result = service.start_setup_token()
    assert result.oauth_url == _FAKE_URL


def test_start_setup_token_raises_on_eof_before_url() -> None:
    fake_process = FakePexpectProcess([(1, "crashed")])
    service = claude_auth.ClaudeAuthService(pexpect_spawner=lambda *_a, **_k: fake_process)
    with pytest.raises(claude_auth.ClaudeAuthError, match="exited before printing"):
        service.start_setup_token()


def test_start_setup_token_raises_on_timeout_waiting_for_url() -> None:
    fake_process = FakePexpectProcess([(2, "")])
    service = claude_auth.ClaudeAuthService(pexpect_spawner=lambda *_a, **_k: fake_process)
    with pytest.raises(claude_auth.ClaudeAuthError, match="Timed out"):
        service.start_setup_token()


def test_poll_setup_token_pending_then_completes(isolated_claude_config: Path) -> None:
    """The normal browser-approval flow: pending polls, then the token appears.

    Completion writes the token into the settings env block and restarts
    the claude agents before returning the final status.
    """
    command_log: list[tuple[str, ...]] = []

    def _runner(cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        command_log.append(tuple(cmd))
        if cmd[1] == "list":
            return FakeFinishedProcess(stdout=_LIST_PAYLOAD)
        return FakeFinishedProcess(returncode=0, stdout='{"loggedIn": true, "authMethod": "oauth_token"}')

    fake_process = FakePexpectProcess(
        [
            (0, _FAKE_URL),
            (2, ""),
            (0, f"Your OAuth token (valid for 1 year):\n{_FAKE_TOKEN}\n"),
        ]
    )
    service = claude_auth.ClaudeAuthService(command_runner=_runner, pexpect_spawner=lambda *_a, **_k: fake_process)
    start = service.start_setup_token()

    pending = service.poll_setup_token(start.session_id)
    assert pending.is_complete is False
    assert command_log == []

    complete = service.poll_setup_token(start.session_id)
    assert complete.is_complete is True
    assert complete.status is not None
    assert complete.status.auth_mode is claude_auth.AuthMode.SUBSCRIPTION
    settings = json.loads((isolated_claude_config / "settings.json").read_text())
    assert settings["env"] == {"CLAUDE_CODE_OAUTH_TOKEN": _FAKE_TOKEN}
    assert any(cmd[1] == "stop" for cmd in command_log)
    # The session is consumed: a further poll must reject the id.
    with pytest.raises(claude_auth.ClaudeAuthError, match="No active setup-token session"):
        service.poll_setup_token(start.session_id)


def test_poll_setup_token_raises_when_subprocess_dies_without_token() -> None:
    fake_process = FakePexpectProcess([(0, _FAKE_URL), (1, "some crash output")])
    service = claude_auth.ClaudeAuthService(pexpect_spawner=lambda *_a, **_k: fake_process)
    start = service.start_setup_token()
    with pytest.raises(claude_auth.ClaudeAuthError, match="exited without printing a token"):
        service.poll_setup_token(start.session_id)
    # The dead session was dropped, so a retry poll reports no session.
    with pytest.raises(claude_auth.ClaudeAuthError, match="No active setup-token session"):
        service.poll_setup_token(start.session_id)


def test_poll_setup_token_rejects_unknown_session() -> None:
    service = claude_auth.ClaudeAuthService()
    with pytest.raises(claude_auth.ClaudeAuthError, match="No active setup-token session"):
        service.poll_setup_token("bogus")


def test_submit_setup_token_code_drives_subprocess_and_completes(isolated_claude_config: Path) -> None:
    command_log: list[tuple[str, ...]] = []

    def _runner(cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        command_log.append(tuple(cmd))
        if cmd[1] == "list":
            return FakeFinishedProcess(stdout=_LIST_PAYLOAD)
        return FakeFinishedProcess(returncode=0, stdout='{"loggedIn": true, "authMethod": "oauth_token"}')

    fake_process = FakePexpectProcess([(0, _FAKE_URL), (0, f"token:\n{_FAKE_TOKEN}\n")])
    service = claude_auth.ClaudeAuthService(command_runner=_runner, pexpect_spawner=lambda *_a, **_k: fake_process)
    start = service.start_setup_token()
    status = service.submit_setup_token_code(start.session_id, "FAKE#CODE")
    assert fake_process.sendline_calls == ["FAKE#CODE"]
    assert status.auth_mode is claude_auth.AuthMode.SUBSCRIPTION
    settings = json.loads((isolated_claude_config / "settings.json").read_text())
    assert settings["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == _FAKE_TOKEN


def test_submit_setup_token_code_rejects_unknown_session() -> None:
    service = claude_auth.ClaudeAuthService()
    with pytest.raises(claude_auth.ClaudeAuthError, match="No active setup-token session"):
        service.submit_setup_token_code("bogus", "fake#code")


def test_abort_setup_token_clears_session() -> None:
    fake_process = FakePexpectProcess([(0, _FAKE_URL)])
    service = claude_auth.ClaudeAuthService(pexpect_spawner=lambda *_a, **_k: fake_process)
    start = service.start_setup_token()
    service.abort_setup_token()
    assert fake_process.terminate_calls >= 1
    with pytest.raises(claude_auth.ClaudeAuthError, match="No active setup-token session"):
        service.poll_setup_token(start.session_id)


# ----- token/URL extraction -----


def test_extract_setup_token_from_ansi_wrapped_output() -> None:
    raw = f"\x1b[1m Your OAuth token (valid for 1 year):\x1b[22m\n \x1b[32m{_FAKE_TOKEN}\x1b[39m\n"
    assert claude_auth._extract_setup_token(raw) == _FAKE_TOKEN


def test_extract_setup_token_returns_none_without_token() -> None:
    assert claude_auth._extract_setup_token("Opening browser to sign in...") is None


def test_extract_oauth_url_returns_none_when_no_url_present() -> None:
    assert claude_auth._extract_oauth_url("no links here") is None


# ----- repo<->mngr CLI contract -----
# These assert the argv shapes we hand to subprocesses are accepted by the
# LIVE mngr CLI (parse-only), so a CLI flag rename breaks these tests
# instead of runtime behavior in a deployed mind.


def test_list_argv_accepted_by_live_cli() -> None:
    assert_mngr_argv_valid(claude_auth._build_list_command())


def test_stop_argv_accepted_by_live_cli() -> None:
    assert_mngr_argv_valid(claude_auth._build_stop_command("some-agent"))


def test_start_argv_accepted_by_live_cli() -> None:
    assert_mngr_argv_valid(claude_auth._build_start_command("some-agent"))


def test_message_argv_accepted_by_live_cli() -> None:
    assert_mngr_argv_valid(claude_auth._build_message_command("some-agent", "please continue"))
