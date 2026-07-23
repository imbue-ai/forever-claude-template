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
import threading
from pathlib import Path

import pytest
from mngr_cli_contract.contract import assert_mngr_argv_valid

from imbue.mngr.cli.exit_codes import EXIT_CODE_PROVIDER_INACCESSIBLE
from imbue.system_interface import claude_auth
from imbue.system_interface.testing import FakeFinishedProcess
from imbue.system_interface.testing import FakePexpectProcess
from imbue.system_interface.testing import wait_for_background_apply

_FAKE_URL = "https://claude.com/cai/oauth/authorize?code=true&state=abc"
_FAKE_TOKEN = "sk-ant-oat01-" + "FAKETOKEN0" * 9 + "12345"


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


def test_restart_issues_one_fused_batched_call_per_behavior_group(
    isolated_claude_config: Path,
) -> None:
    """The full restart contract in one pass.

    STOPPED agents are untouched; previously-RUNNING agents (chats and
    workers alike) restart in ONE fused call that delivers the auth-aware
    continue message via mngr's resume machinery; previously-WAITING
    agents restart in a second fused call with --no-resume. Nothing
    outside the settings env is touched (no .claude.json writes).
    """
    command_log: list[tuple[str, ...]] = []
    service = _build_restart_recording_service(command_log)
    restarted = service.restart_all_claude_agents()
    assert restarted == ["chat-1", "worker-1", "chat-2"]
    mngr_calls = [cmd for cmd in command_log if cmd[0] == "mngr" and cmd[1] != "list"]
    assert mngr_calls == [
        (
            "mngr",
            "start",
            "--restart",
            "--resume-message",
            claude_auth.RESTART_CONTINUE_MESSAGE,
            "chat-1",
            "worker-1",
        ),
        ("mngr", "start", "--restart", "--no-resume", "chat-2"),
    ]
    assert not (isolated_claude_config / ".claude.json").exists()


def test_restart_demotes_never_welcomed_agent_to_no_resume(isolated_claude_config: Path) -> None:
    """A never-welcomed chat agent restarts idle even when it snapshots as RUNNING.

    A fresh workspace's failed pre-auth /welcome ends in an API error that
    fires no Stop hook, stranding the `active` marker -- so the idle agent
    snapshots as RUNNING and would get the "please continue" resume message,
    sending it hunting for nonexistent work. Naming it demotes it to the
    --no-resume group; the post-restart welcome resend is its resumption.
    """
    command_log: list[tuple[str, ...]] = []
    service = _build_restart_recording_service(command_log)

    restarted = service.restart_all_claude_agents(never_welcomed_agent_name="chat-1")

    assert set(restarted) == {"chat-1", "worker-1", "chat-2"}
    mngr_calls = [cmd for cmd in command_log if cmd[0] == "mngr" and cmd[1] != "list"]
    assert mngr_calls == [
        (
            "mngr",
            "start",
            "--restart",
            "--resume-message",
            claude_auth.RESTART_CONTINUE_MESSAGE,
            "worker-1",
        ),
        ("mngr", "start", "--restart", "--no-resume", "chat-2", "chat-1"),
    ]


def test_background_apply_consults_never_welcomed_resolver(isolated_claude_config: Path) -> None:
    """The apply thread resolves the never-welcomed agent and suppresses its resume message."""
    command_log: list[tuple[str, ...]] = []

    def _runner(cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        command_log.append(tuple(cmd))
        if cmd[1] == "list":
            return FakeFinishedProcess(stdout=_LIST_PAYLOAD)
        return FakeFinishedProcess(returncode=0, stdout='{"loggedIn": true}')

    service = claude_auth.ClaudeAuthService(
        command_runner=_runner,
        resolve_never_welcomed_agent_name=lambda: "chat-1",
    )
    service.submit_credentials("ANTHROPIC_API_KEY=sk-ant-fresh", None)
    progress = wait_for_background_apply(service)

    assert progress.phase is claude_auth.RestartPhase.DONE
    resume_calls = [cmd for cmd in command_log if "--resume-message" in cmd]
    no_resume_calls = [cmd for cmd in command_log if "--no-resume" in cmd]
    assert all("chat-1" not in cmd for cmd in resume_calls)
    assert any("chat-1" in cmd for cmd in no_resume_calls)


def test_restart_raises_when_start_fails(isolated_claude_config: Path) -> None:
    def _runner(cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        if cmd[1] == "list":
            return FakeFinishedProcess(stdout=_LIST_PAYLOAD)
        if cmd[1] == "start":
            return FakeFinishedProcess(returncode=1, stderr="start broke")
        return FakeFinishedProcess(returncode=0)

    service = claude_auth.ClaudeAuthService(command_runner=_runner)
    with pytest.raises(claude_auth.ClaudeAuthError, match="mngr start"):
        service.restart_all_claude_agents()


# ----- submit_credentials -----


def test_submit_credentials_writes_settings_and_restarts_in_background(isolated_claude_config: Path) -> None:
    command_log: list[tuple[str, ...]] = []
    service = _build_restart_recording_service(command_log)
    completions: list[str] = []
    status = service.submit_credentials("ANTHROPIC_API_KEY=sk-ant-fresh", lambda: completions.append("done"))
    # The submit returns immediately with the apply's initial progress.
    assert status.restart_phase == claude_auth.RestartPhase.RESTARTING.value
    assert status.restart_reason == claude_auth.RestartReason.CREDENTIALS_SAVED.value
    assert status.auth_mode is claude_auth.AuthMode.API_KEY
    progress = wait_for_background_apply(service)
    assert progress.phase is claude_auth.RestartPhase.DONE
    settings = json.loads((isolated_claude_config / "settings.json").read_text())
    assert settings["env"] == {"ANTHROPIC_API_KEY": "sk-ant-fresh"}
    assert any(cmd[1] == "start" and "--restart" in cmd for cmd in command_log)
    # The on-complete hook (welcome resend) ran after the restart finished.
    assert completions == ["done"]
    # The apply also recorded the key's approval so the restarted interactive
    # claude skips the "Do you want to use this API key?" challenge (which
    # would otherwise block the agent and the welcome dispatch).
    claude_json = json.loads((isolated_claude_config / ".claude.json").read_text())
    assert claude_json["customApiKeyResponses"] == {"approved": ["sk-ant-fresh"], "rejected": []}


def test_submit_credentials_reports_failed_phase_when_restart_fails(isolated_claude_config: Path) -> None:
    def _runner(cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        if cmd[1] == "list":
            return FakeFinishedProcess(stdout=_LIST_PAYLOAD)
        if cmd[1] == "start":
            return FakeFinishedProcess(returncode=1, stderr="start broke")
        return FakeFinishedProcess(returncode=0, stdout='{"loggedIn": true}')

    service = claude_auth.ClaudeAuthService(command_runner=_runner)
    service.submit_credentials("ANTHROPIC_API_KEY=sk-ant-fresh", None)
    progress = wait_for_background_apply(service)
    assert progress.phase is claude_auth.RestartPhase.FAILED
    assert progress.error is not None and "mngr start" in progress.error


def test_submit_credentials_rejects_second_change_while_apply_is_running(isolated_claude_config: Path) -> None:
    release_start = threading.Event()

    def _runner(cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        if cmd[1] == "list":
            return FakeFinishedProcess(stdout=_LIST_PAYLOAD)
        if cmd[1] == "start":
            release_start.wait(timeout=10)
        return FakeFinishedProcess(returncode=0, stdout='{"loggedIn": true}')

    service = claude_auth.ClaudeAuthService(command_runner=_runner)
    service.submit_credentials("ANTHROPIC_API_KEY=sk-ant-first", None)
    with pytest.raises(claude_auth.ClaudeAuthError, match="still in progress"):
        service.submit_credentials("ANTHROPIC_API_KEY=sk-ant-second", None)
    release_start.set()
    wait_for_background_apply(service)


def test_submit_credentials_rejects_bad_paste_without_touching_anything(isolated_claude_config: Path) -> None:
    command_log: list[tuple[str, ...]] = []
    service = _build_restart_recording_service(command_log)
    with pytest.raises(claude_auth.CredentialPasteError):
        service.submit_credentials("NOT_A_MANAGED_KEY=x", None)
    assert not (isolated_claude_config / "settings.json").exists()
    assert command_log == []


# ----- record_api_key_approval -----


def test_record_api_key_approval_appends_suffix_and_preserves_existing(tmp_path: Path) -> None:
    """The key's last-20-char suffix joins the approved list; unrelated config and prior approvals survive."""
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(
        json.dumps(
            {
                "hasCompletedOnboarding": True,
                "customApiKeyResponses": {"approved": ["old-approval-entry"], "rejected": ["rejected-once"]},
            }
        )
    )
    api_key = "sk-ant-api03-" + "x" * 30

    claude_auth.record_api_key_approval({"ANTHROPIC_API_KEY": api_key}, claude_json_path_override=claude_json)

    data = json.loads(claude_json.read_text())
    assert data["hasCompletedOnboarding"] is True
    assert data["customApiKeyResponses"]["approved"] == ["old-approval-entry", api_key[-20:]]
    # Mirrors mngr's approve_api_key_for_claude: a stale rejection would keep
    # suppressing the key, so the rejected list is reset.
    assert data["customApiKeyResponses"]["rejected"] == []

    # Idempotent: a second apply of the same key adds no duplicate.
    claude_auth.record_api_key_approval({"ANTHROPIC_API_KEY": api_key}, claude_json_path_override=claude_json)
    assert json.loads(claude_json.read_text())["customApiKeyResponses"]["approved"] == [
        "old-approval-entry",
        api_key[-20:],
    ]


def test_record_api_key_approval_is_noop_without_api_key(tmp_path: Path) -> None:
    """Token-mode (and clearing) applies carry no API key, so `.claude.json` is untouched."""
    claude_json = tmp_path / ".claude.json"

    claude_auth.record_api_key_approval({"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-token"}, claude_json)
    claude_auth.record_api_key_approval({}, claude_json)

    assert not claude_json.exists()


def test_record_api_key_approval_creates_missing_claude_json(tmp_path: Path) -> None:
    """A mind whose `.claude.json` does not exist yet still gets the approval recorded."""
    claude_json = tmp_path / ".claude.json"

    claude_auth.record_api_key_approval({"ANTHROPIC_API_KEY": "sk-ant-fresh"}, claude_json)

    assert json.loads(claude_json.read_text())["customApiKeyResponses"] == {
        "approved": ["sk-ant-fresh"],
        "rejected": [],
    }


def test_background_apply_fails_loudly_on_corrupt_claude_json(isolated_claude_config: Path) -> None:
    """A corrupt `.claude.json` fails the apply instead of restarting into the interactive challenge."""
    (isolated_claude_config / ".claude.json").write_text("not json {{{")
    command_log: list[tuple[str, ...]] = []
    service = _build_restart_recording_service(command_log)

    service.submit_credentials("ANTHROPIC_API_KEY=sk-ant-fresh", None)
    progress = wait_for_background_apply(service)

    assert progress.phase is claude_auth.RestartPhase.FAILED
    assert progress.error is not None and "corrupt" in progress.error
    # The apply failed before the restart step.
    assert not any(cmd[1] == "start" for cmd in command_log)


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

    Completion writes the token into the settings env block and hands the
    agent restart to the background apply. Pump pattern order: token=0,
    OAuth-error=1, EOF=2, TIMEOUT=3.
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
            (3, ""),
            (0, f"Your OAuth token (valid for 1 year):\r\n{_FAKE_TOKEN}\r\nStore this token securely.\r\n"),
        ]
    )
    service = claude_auth.ClaudeAuthService(command_runner=_runner, pexpect_spawner=lambda *_a, **_k: fake_process)
    start = service.start_setup_token()

    pending = service.poll_setup_token(start.session_id, None)
    assert pending.is_complete is False
    assert command_log == []

    complete = service.poll_setup_token(start.session_id, None)
    assert complete.is_complete is True
    assert complete.status is not None
    assert complete.status.auth_mode is claude_auth.AuthMode.SUBSCRIPTION
    progress = wait_for_background_apply(service)
    assert progress.phase is claude_auth.RestartPhase.DONE
    settings = json.loads((isolated_claude_config / "settings.json").read_text())
    assert settings["env"] == {"CLAUDE_CODE_OAUTH_TOKEN": _FAKE_TOKEN}
    assert any(cmd[0] == "mngr" and cmd[1] == "start" for cmd in command_log)
    # The session is consumed: a further poll must reject the id.
    with pytest.raises(claude_auth.ClaudeAuthError, match="No active setup-token session"):
        service.poll_setup_token(start.session_id, None)


def test_poll_setup_token_raises_when_subprocess_dies_without_token() -> None:
    fake_process = FakePexpectProcess([(0, _FAKE_URL), (2, "some crash output")])
    service = claude_auth.ClaudeAuthService(pexpect_spawner=lambda *_a, **_k: fake_process)
    start = service.start_setup_token()
    with pytest.raises(claude_auth.ClaudeAuthError, match="exited without printing a token"):
        service.poll_setup_token(start.session_id, None)
    # The dead session was dropped, so a retry poll reports no session.
    with pytest.raises(claude_auth.ClaudeAuthError, match="No active setup-token session"):
        service.poll_setup_token(start.session_id, None)


def test_poll_setup_token_fails_fast_on_oauth_error() -> None:
    fake_process = FakePexpectProcess([(0, _FAKE_URL), (1, "OAuth error: Request failed with status code 400")])
    service = claude_auth.ClaudeAuthService(pexpect_spawner=lambda *_a, **_k: fake_process)
    start = service.start_setup_token()
    with pytest.raises(claude_auth.ClaudeAuthError, match="OAuth error"):
        service.poll_setup_token(start.session_id, None)


def test_poll_setup_token_rejects_unknown_session() -> None:
    service = claude_auth.ClaudeAuthService()
    with pytest.raises(claude_auth.ClaudeAuthError, match="No active setup-token session"):
        service.poll_setup_token("bogus", None)


def test_submit_setup_token_code_drives_subprocess_and_completes(isolated_claude_config: Path) -> None:
    command_log: list[tuple[str, ...]] = []

    def _runner(cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        command_log.append(tuple(cmd))
        if cmd[1] == "list":
            return FakeFinishedProcess(stdout=_LIST_PAYLOAD)
        return FakeFinishedProcess(returncode=0, stdout='{"loggedIn": true, "authMethod": "oauth_token"}')

    fake_process = FakePexpectProcess([(0, _FAKE_URL), (0, f"token:\r\n{_FAKE_TOKEN}\r\nDone.\r\n")])
    service = claude_auth.ClaudeAuthService(command_runner=_runner, pexpect_spawner=lambda *_a, **_k: fake_process)
    start = service.start_setup_token()
    status = service.submit_setup_token_code(start.session_id, "FAKE#CODE", None)
    # The code is typed and Enter delivered as its own deferred keystroke
    # (a single sendline would be swallowed by the CLI's paste heuristic).
    assert fake_process.send_calls == ["FAKE#CODE", "\r"]
    assert status.auth_mode is claude_auth.AuthMode.SUBSCRIPTION
    progress = wait_for_background_apply(service)
    assert progress.phase is claude_auth.RestartPhase.DONE
    settings = json.loads((isolated_claude_config / "settings.json").read_text())
    assert settings["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == _FAKE_TOKEN


def test_submit_setup_token_code_rejects_unknown_session() -> None:
    service = claude_auth.ClaudeAuthService()
    with pytest.raises(claude_auth.ClaudeAuthError, match="No active setup-token session"):
        service.submit_setup_token_code("bogus", "fake#code", None)


def test_abort_auth_flow_clears_session() -> None:
    fake_process = FakePexpectProcess([(0, _FAKE_URL)])
    service = claude_auth.ClaudeAuthService(pexpect_spawner=lambda *_a, **_k: fake_process)
    start = service.start_setup_token()
    service.abort_auth_flow()
    assert fake_process.terminate_calls >= 1
    with pytest.raises(claude_auth.ClaudeAuthError, match="No active setup-token session"):
        service.poll_setup_token(start.session_id, None)


# ----- browser sign-in (claude auth login) flow -----
# Oauth pump pattern order: success=0, failed=1, OAuth-error=2, EOF=3, TIMEOUT=4.


def test_start_oauth_login_spawns_provider_flag_and_extracts_url() -> None:
    spawn_args: list[tuple[str, list[str]]] = []

    def _spawner(executable: str, args: list[str], _timeout: float) -> FakePexpectProcess:
        spawn_args.append((executable, args))
        return FakePexpectProcess([(0, _FAKE_URL)])

    service = claude_auth.ClaudeAuthService(pexpect_spawner=_spawner)
    result = service.start_oauth_login(claude_auth.OAuthProvider.CLAUDEAI)
    assert result.oauth_url == _FAKE_URL
    assert spawn_args == [("claude", ["auth", "login", "--claudeai"])]

    service.abort_auth_flow()
    service_console = claude_auth.ClaudeAuthService(pexpect_spawner=_spawner)
    service_console.start_oauth_login(claude_auth.OAuthProvider.CONSOLE)
    assert spawn_args[-1] == ("claude", ["auth", "login", "--console"])


def test_oauth_login_fast_path_completes_without_restart(isolated_claude_config: Path) -> None:
    """Subscription sign-in with an empty managed env: the credential is
    stored by the CLI and re-read live, so nothing restarts and the
    welcome hook runs inline."""
    command_log: list[tuple[str, ...]] = []

    def _runner(cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        command_log.append(tuple(cmd))
        if cmd[1] == "list":
            return FakeFinishedProcess(stdout=_LIST_PAYLOAD)
        return FakeFinishedProcess(
            returncode=0,
            stdout='{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "Max", "email": "x@y.com"}',
        )

    fake_process = FakePexpectProcess([(0, _FAKE_URL), (4, ""), (0, "Login successful.\r\n")])
    service = claude_auth.ClaudeAuthService(command_runner=_runner, pexpect_spawner=lambda *_a, **_k: fake_process)
    start = service.start_oauth_login(claude_auth.OAuthProvider.CLAUDEAI)

    pending = service.poll_oauth_login(start.session_id, None)
    assert pending.is_complete is False

    completions: list[str] = []
    complete = service.poll_oauth_login(start.session_id, lambda: completions.append("welcome"))
    assert complete.is_complete is True
    assert complete.status is not None
    assert complete.status.auth_mode is claude_auth.AuthMode.SUBSCRIPTION
    assert complete.status.email == "x@y.com"
    assert complete.status.restart_phase is None
    assert completions == ["welcome"]
    assert all(cmd[0] != "mngr" or cmd[1] == "list" for cmd in command_log)
    assert not (isolated_claude_config / "settings.json").exists()


def test_oauth_login_fast_path_clears_stale_failed_restart_progress(isolated_claude_config: Path) -> None:
    """A FAILED restart left by an earlier credential change (which emptied
    the managed env, so this sign-in takes the fast path) must not leak into
    the fast-path completion status: the frontend routes restart_phase
    'failed' to the error screen, misreporting the successful sign-in."""

    def _runner(cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        if cmd[1] == "list":
            return FakeFinishedProcess(stdout=_LIST_PAYLOAD)
        if cmd[1] == "start":
            return FakeFinishedProcess(returncode=1, stderr="start broke")
        return FakeFinishedProcess(
            returncode=0, stdout='{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "Max"}'
        )

    fake_process = FakePexpectProcess([(0, _FAKE_URL), (0, "Login successful.\r\n")])
    service = claude_auth.ClaudeAuthService(command_runner=_runner, pexpect_spawner=lambda *_a, **_k: fake_process)
    # A subscription-switch apply clears the managed env, then its restart fails.
    service.start_background_apply({}, None, claude_auth.RestartReason.SUBSCRIPTION_SWITCH)
    progress = wait_for_background_apply(service)
    assert progress.phase is claude_auth.RestartPhase.FAILED

    start = service.start_oauth_login(claude_auth.OAuthProvider.CLAUDEAI)
    complete = service.poll_oauth_login(start.session_id, None)
    assert complete.is_complete is True
    assert complete.status is not None
    assert complete.status.auth_mode is claude_auth.AuthMode.SUBSCRIPTION
    assert complete.status.restart_phase is None
    assert complete.status.restart_error is None


def test_oauth_login_with_managed_keys_clears_them_and_restarts(isolated_claude_config: Path) -> None:
    """The switching case: active managed keys outrank the fresh credential,
    so they are cleared and the agents restarted in the background."""
    claude_auth.write_managed_auth_env({"ANTHROPIC_API_KEY": "sk-ant-old"})
    command_log: list[tuple[str, ...]] = []

    def _runner(cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        command_log.append(tuple(cmd))
        if cmd[1] == "list":
            return FakeFinishedProcess(stdout=_LIST_PAYLOAD)
        return FakeFinishedProcess(
            returncode=0, stdout='{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "Max"}'
        )

    fake_process = FakePexpectProcess([(0, _FAKE_URL), (0, "Login successful.\r\n")])
    service = claude_auth.ClaudeAuthService(command_runner=_runner, pexpect_spawner=lambda *_a, **_k: fake_process)
    start = service.start_oauth_login(claude_auth.OAuthProvider.CLAUDEAI)
    complete = service.poll_oauth_login(start.session_id, None)
    assert complete.is_complete is True
    assert complete.status is not None
    assert complete.status.restart_reason == claude_auth.RestartReason.SUBSCRIPTION_SWITCH.value
    progress = wait_for_background_apply(service)
    assert progress.phase is claude_auth.RestartPhase.DONE
    settings = json.loads((isolated_claude_config / "settings.json").read_text())
    assert settings.get("env", {}) == {}
    assert any(cmd[0] == "mngr" and cmd[1] == "start" for cmd in command_log)


def test_oauth_login_console_always_restarts(isolated_claude_config: Path) -> None:
    """Console's key lands in .claude.json (cached at claude start), so it
    takes the restart path even with an empty managed env."""

    def _runner(cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        if cmd[1] == "list":
            return FakeFinishedProcess(stdout=_LIST_PAYLOAD)
        return FakeFinishedProcess(returncode=0, stdout='{"loggedIn": true, "authMethod": "claude.ai"}')

    fake_process = FakePexpectProcess([(0, _FAKE_URL), (0, "Login successful.\r\n")])
    service = claude_auth.ClaudeAuthService(command_runner=_runner, pexpect_spawner=lambda *_a, **_k: fake_process)
    start = service.start_oauth_login(claude_auth.OAuthProvider.CONSOLE)
    complete = service.poll_oauth_login(start.session_id, None)
    assert complete.is_complete is True
    assert complete.status is not None
    assert complete.status.restart_reason == claude_auth.RestartReason.CONSOLE_SWITCH.value
    progress = wait_for_background_apply(service)
    assert progress.phase is claude_auth.RestartPhase.DONE


def test_oauth_login_surfaces_login_failed_detail() -> None:
    fake_process = FakePexpectProcess([(0, _FAKE_URL), (1, "Login failed: token exchange rejected\r\n")])
    service = claude_auth.ClaudeAuthService(pexpect_spawner=lambda *_a, **_k: fake_process)
    start = service.start_oauth_login(claude_auth.OAuthProvider.CLAUDEAI)
    with pytest.raises(claude_auth.ClaudeAuthError, match="token exchange rejected"):
        service.poll_oauth_login(start.session_id, None)


def test_submit_oauth_login_code_completes_fast_path(isolated_claude_config: Path) -> None:
    def _runner(cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        if cmd[1] == "list":
            return FakeFinishedProcess(stdout=_LIST_PAYLOAD)
        return FakeFinishedProcess(
            returncode=0, stdout='{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "Pro"}'
        )

    fake_process = FakePexpectProcess([(0, _FAKE_URL), (0, "Login successful.\r\n")])
    service = claude_auth.ClaudeAuthService(command_runner=_runner, pexpect_spawner=lambda *_a, **_k: fake_process)
    start = service.start_oauth_login(claude_auth.OAuthProvider.CLAUDEAI)
    status = service.submit_oauth_login_code(start.session_id, "FAKE#CODE", None)
    assert fake_process.send_calls == ["FAKE#CODE", "\r"]
    assert status.auth_mode is claude_auth.AuthMode.SUBSCRIPTION
    assert status.restart_phase is None


def test_oauth_and_setup_token_sessions_do_not_cross_match() -> None:
    fake_process = FakePexpectProcess([(0, _FAKE_URL)])
    service = claude_auth.ClaudeAuthService(pexpect_spawner=lambda *_a, **_k: fake_process)
    start = service.start_oauth_login(claude_auth.OAuthProvider.CLAUDEAI)
    with pytest.raises(claude_auth.ClaudeAuthError, match="No active setup-token session"):
        service.poll_setup_token(start.session_id, None)


# ----- credentials-based mode folding -----


def test_status_folds_subscription_mode_from_credentials_when_env_empty(isolated_claude_config: Path) -> None:
    def _runner(_cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        return FakeFinishedProcess(
            stdout='{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "Max", "email": "x@y.com"}'
        )

    service = claude_auth.ClaudeAuthService(command_runner=_runner)
    status = service.get_auth_status()
    assert status.auth_mode is claude_auth.AuthMode.SUBSCRIPTION


def test_status_folds_console_mode_when_claude_ai_without_subscription(isolated_claude_config: Path) -> None:
    def _runner(_cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        return FakeFinishedProcess(stdout='{"loggedIn": true, "authMethod": "claude.ai"}')

    service = claude_auth.ClaudeAuthService(command_runner=_runner)
    status = service.get_auth_status()
    assert status.auth_mode is claude_auth.AuthMode.CONSOLE


def test_status_managed_env_outranks_credentials_fold(isolated_claude_config: Path) -> None:
    (isolated_claude_config / "settings.json").write_text(json.dumps({"env": {"ANTHROPIC_API_KEY": "sk-ant-key"}}))

    def _runner(_cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        return FakeFinishedProcess(
            stdout='{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "Max"}'
        )

    service = claude_auth.ClaudeAuthService(command_runner=_runner)
    assert service.get_auth_status().auth_mode is claude_auth.AuthMode.API_KEY


# ----- token/URL extraction -----


def test_extract_setup_token_from_ansi_wrapped_output() -> None:
    raw = f"\x1b[1m Your OAuth token (valid for 1 year):\x1b[22m\r\n \x1b[32m{_FAKE_TOKEN}\x1b[39m\r\n"
    assert claude_auth._extract_setup_token(raw) == _FAKE_TOKEN


def test_extract_setup_token_reassembles_width_wrapped_rows() -> None:
    """A ~108-char token hard-wraps at the 80-column PTY; the screen replay
    must join the physically adjacent rows exactly."""
    raw = f"Your OAuth token (valid for 1 year):\r\n{_FAKE_TOKEN[:80]}\r\n{_FAKE_TOKEN[80:]}\r\nStore this token securely.\r\n"
    assert claude_auth._extract_setup_token(raw) == _FAKE_TOKEN


def test_extract_setup_token_ignores_stale_frame_under_first_row() -> None:
    """Regression: a mid-render frame can show the token's first row over the
    PREVIOUS frame's content; the fully drawn later frame must win (the old
    extractor stored an 80-column stump here)."""
    frame_end = "\x1b[?2026l"
    raw = (
        "\x1b[2J\x1b[1;1H" + _FAKE_TOKEN[:80] + "\x1b[2;1H" + "esponse_type=code&redirect_uri=stale" + frame_end
        + "\x1b[2;1H\x1b[K" + _FAKE_TOKEN[80:] + "\x1b[3;1H\x1b[KStore this token securely." + frame_end
    )
    assert claude_auth._extract_setup_token(raw) == _FAKE_TOKEN


def test_extract_setup_token_survives_screen_clear_at_exit() -> None:
    raw = f"token:\r\n{_FAKE_TOKEN[:80]}\r\n{_FAKE_TOKEN[80:]}\r\nDone.\r\n\x1b[?2026l\x1b[2J\x1b[1;1H\x1b[?2026l"
    assert claude_auth._extract_setup_token(raw) == _FAKE_TOKEN


def test_extract_setup_token_refuses_short_fragments() -> None:
    assert claude_auth._extract_setup_token("sk-ant-oat01-tooshort\r\nDone.\r\n") is None


def test_extract_setup_token_returns_none_without_token() -> None:
    assert claude_auth._extract_setup_token("Opening browser to sign in...") is None


def test_extract_oauth_url_prefers_osc8_hyperlink_target_over_garbled_label() -> None:
    """The CLI's visible wrapped label render is garbled; the OSC 8 target
    (id-parameterized, BEL-terminated) carries the intact URL."""
    full_url = _FAKE_URL + "&redirect_uri=https%3A%2F%2Fx&state=S123"
    raw = f"\x1b]8;id=1abc;{full_url}\x07\x1b[38;5;246m{full_url[:80]}\x1b[39m\x1b]8;;\x07"
    assert claude_auth._extract_oauth_url(raw) == full_url


def test_extract_oauth_url_returns_none_when_no_url_present() -> None:
    assert claude_auth._extract_oauth_url("no links here") is None


# ----- repo<->mngr CLI contract -----
# These assert the argv shapes we hand to subprocesses are accepted by the
# LIVE mngr CLI (parse-only), so a CLI flag rename breaks these tests
# instead of runtime behavior in a deployed mind.


def test_list_argv_accepted_by_live_cli() -> None:
    assert_mngr_argv_valid(claude_auth._build_list_command())


def test_restart_with_message_argv_accepted_by_live_cli() -> None:
    assert_mngr_argv_valid(
        claude_auth._build_restart_with_message_command(["agent-a", "agent-b"], "please continue")
    )


def test_restart_no_resume_argv_accepted_by_live_cli() -> None:
    assert_mngr_argv_valid(claude_auth._build_restart_no_resume_command(["agent-a"]))
