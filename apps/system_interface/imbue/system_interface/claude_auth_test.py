"""Tests for the claude_auth backend module.

`ClaudeAuthService` takes its outside-world dependencies (`command_runner`,
`pexpect_spawner`) as constructor arguments, so each test builds an
isolated instance with deterministic fakes -- no `unittest.mock`, and no
runtime patching of module attributes. The pure module-level helpers
(`_parse_status_payload`, `_format_env_file`, `write_api_key_to_host_env`,
the config-prep helpers, the OAuth URL regex) are tested directly.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest
from mngr_cli_contract.contract import assert_mngr_argv_valid
from pydantic import SecretStr

from imbue.mngr.cli.exit_codes import EXIT_CODE_PROVIDER_INACCESSIBLE
from imbue.system_interface import claude_auth
from imbue.system_interface.testing import FakeFinishedProcess
from imbue.system_interface.testing import FakePexpectProcess


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


def test_get_auth_status_returns_logged_out_when_runner_raises() -> None:
    def _runner(_cmd: list[str], _timeout: float) -> FakeFinishedProcess:
        raise claude_auth.ProcessSetupError(
            command=("claude",), stdout="", stderr="not found", is_output_already_logged=False
        )

    service = claude_auth.ClaudeAuthService(command_runner=_runner)
    status = service.get_auth_status()
    assert status.logged_in is False


def test_get_auth_status_parses_logged_in_json() -> None:
    def _runner(_cmd: list[str], _timeout: float) -> FakeFinishedProcess:
        return FakeFinishedProcess(stdout='{"loggedIn": true, "email": "x@y.com", "subscriptionType": "Pro"}')

    service = claude_auth.ClaudeAuthService(command_runner=_runner)
    status = service.get_auth_status()
    assert status.logged_in is True
    assert status.email == "x@y.com"
    assert status.subscription_type == "Pro"


def test_get_auth_status_rejects_non_json_output() -> None:
    def _runner(_cmd: list[str], _timeout: float) -> FakeFinishedProcess:
        return FakeFinishedProcess(stdout="not json at all")

    service = claude_auth.ClaudeAuthService(command_runner=_runner)
    with pytest.raises(claude_auth.ClaudeAuthError, match="non-JSON"):
        service.get_auth_status()


def test_get_auth_status_treats_empty_output_as_logged_out() -> None:
    def _runner(_cmd: list[str], _timeout: float) -> FakeFinishedProcess:
        return FakeFinishedProcess(stdout="")

    service = claude_auth.ClaudeAuthService(command_runner=_runner)
    status = service.get_auth_status()
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
    service = claude_auth.ClaudeAuthService()
    with pytest.raises(claude_auth.ClaudeAuthError, match="No active OAuth session"):
        service.submit_oauth_code("bogus", "fake#code")


def test_oauth_session_extracts_url_from_spawner_stdout() -> None:
    fake_url = "https://claude.ai/oauth/authorize?code=abc&state=def"
    fake_process = FakePexpectProcess(url_match=fake_url, expect_return_index=0)
    service = claude_auth.ClaudeAuthService(pexpect_spawner=lambda *_args, **_kwargs: fake_process)
    result = service.start_oauth_login(claude_auth.OAuthProvider.CLAUDEAI)
    assert result.oauth_url == fake_url
    assert result.session_id


def _osc8_hyperlink(url: str) -> str:
    """Render `url` the way `claude auth login` does: an OSC 8 hyperlink whose
    target *and* blue-styled visible label are both the URL.

    Layout: `ESC]8;;<url>ST <ESC[94m><url><ESC[39m> ESC]8;;ST`. The bare URL
    thus appears twice, wrapped in escape sequences -- the exact stream that
    made the modal copy a doubled, escape-laden link.
    """
    return f"\x1b]8;;{url}\x1b\\\x1b[94m{url}\x1b[39m\x1b]8;;\x1b\\"


# A real authorize URL as emitted by the current CLI (from the reported bug).
_REAL_OAUTH_URL = (
    "https://claude.com/cai/oauth/authorize?code=true"
    "&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e&response_type=code"
    "&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback"
    "&scope=org%3Acreate_api_key+user%3Aprofile&code_challenge=FfOfVy8IPT0c"
    "&code_challenge_method=S256&state=42H_HryL59xS-VHl3yIDzgDwy5Xp29xUXPuoWMmOL8U"
)


def test_extract_oauth_url_collapses_osc8_hyperlink_to_single_clean_url() -> None:
    """The doubled, escape-wrapped OSC 8 hyperlink collapses to one bare URL."""
    wrapped = _osc8_hyperlink(_REAL_OAUTH_URL)
    # Sanity: the raw stream really does carry the URL twice plus escapes,
    # which is what the old regex-only extraction returned verbatim.
    assert wrapped.count(_REAL_OAUTH_URL) == 2
    assert claude_auth._extract_oauth_url(wrapped) == _REAL_OAUTH_URL


def test_extract_oauth_url_returns_none_when_no_url_present() -> None:
    assert claude_auth._extract_oauth_url("no link in this output\n") is None


def test_oauth_session_extracts_clean_url_from_osc8_hyperlink_stream() -> None:
    """End-to-end: a spawner emitting the OSC 8 hyperlink yields one clean URL."""
    fake_process = FakePexpectProcess(raw_output=_osc8_hyperlink(_REAL_OAUTH_URL), expect_return_index=0)
    service = claude_auth.ClaudeAuthService(pexpect_spawner=lambda *_args, **_kwargs: fake_process)
    result = service.start_oauth_login(claude_auth.OAuthProvider.CLAUDEAI)
    assert result.oauth_url == _REAL_OAUTH_URL


def test_oauth_session_raises_when_url_unextractable_after_stripping() -> None:
    fake_process = FakePexpectProcess(
        raw_output="matched something but no url survives stripping",
        expect_return_index=0,
    )
    service = claude_auth.ClaudeAuthService(pexpect_spawner=lambda *_args, **_kwargs: fake_process)
    with pytest.raises(claude_auth.ClaudeAuthError, match="could not be extracted"):
        service.start_oauth_login(claude_auth.OAuthProvider.CLAUDEAI)


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("https://claude.com/cai/oauth/authorize?code=abc&state=def", id="claudeai-host"),
        pytest.param("https://platform.claude.com/oauth/authorize?code=abc&state=def", id="console-host"),
        pytest.param("https://claude.ai/oauth/authorize?code=abc&state=def", id="legacy-claudeai-host"),
    ],
)
def test_oauth_url_regex_accepts_known_host_forms(url: str) -> None:
    """The regex was loosened to match any host with an oauth/authorize path.

    Guards against an accidental re-tightening that would break the Console
    (`platform.claude.com`) and current `claude.com/cai/...` paths.
    """
    match = claude_auth._OAUTH_URL_REGEX.search(f"prefix\n{url}\nsuffix")
    assert match is not None
    assert match.group(0) == url


def test_oauth_session_raises_on_eof_before_url() -> None:
    fake_process = FakePexpectProcess(url_match=None, expect_return_index=1)
    service = claude_auth.ClaudeAuthService(pexpect_spawner=lambda *_args, **_kwargs: fake_process)
    with pytest.raises(claude_auth.ClaudeAuthError, match="before printing OAuth URL"):
        service.start_oauth_login(claude_auth.OAuthProvider.CLAUDEAI)


def test_oauth_session_raises_on_timeout_waiting_for_url() -> None:
    fake_process = FakePexpectProcess(url_match=None, expect_return_index=2)
    service = claude_auth.ClaudeAuthService(pexpect_spawner=lambda *_args, **_kwargs: fake_process)
    with pytest.raises(claude_auth.ClaudeAuthError, match="Timed out"):
        service.start_oauth_login(claude_auth.OAuthProvider.CLAUDEAI)


def test_list_claude_agent_names_filters_to_claude_type() -> None:
    """Only `type: "claude"` agents are returned; `type: "main"` is skipped.

    The `main`-type agent in a real mind is system-services, which has
    no interactive claude process and would error on `mngr stop`.
    """
    payload = (
        '{"agents": ['
        '{"name": "ababa", "type": "claude"}, '
        '{"name": "system-services", "type": "main"}, '
        '{"name": "feature-x", "type": "claude"}'
        "]}"
    )

    def _runner(cmd: list[str], _timeout: float) -> FakeFinishedProcess:
        assert cmd[:3] == ["mngr", "list", "--format"]
        return FakeFinishedProcess(stdout=payload)

    service = claude_auth.ClaudeAuthService(command_runner=_runner)
    names = service.list_claude_agent_names()
    assert names == ["ababa", "feature-x"]


def test_list_claude_agent_names_raises_on_mngr_failure() -> None:
    def _runner(_cmd: list[str], _timeout: float) -> FakeFinishedProcess:
        return FakeFinishedProcess(stderr="boom", returncode=1)

    service = claude_auth.ClaudeAuthService(command_runner=_runner)
    with pytest.raises(claude_auth.ClaudeAuthError, match="mngr list failed"):
        service.list_claude_agent_names()


def test_build_list_command_includes_on_error_continue() -> None:
    """The blanket listing tolerates unauthenticated providers via --on-error continue."""
    command = claude_auth._build_list_command()
    assert command[-2:] == ["--on-error", "continue"]


def test_list_claude_agent_names_tolerates_provider_inaccessible_exit() -> None:
    """An unauthenticated provider (exit 6) still yields the healthy providers' agents.

    With --on-error continue, `mngr list` emits the reachable agents and exits
    EXIT_CODE_PROVIDER_INACCESSIBLE; that is a benign partial success for this
    blanket listing.
    """
    payload = (
        '{"agents": ['
        '{"name": "ababa", "type": "claude"}, '
        '{"name": "feature-x", "type": "claude"}'
        '], "errors": [{"provider_name": "modal", "message": "not authenticated"}]}'
    )

    def _runner(_cmd: list[str], _timeout: float) -> FakeFinishedProcess:
        return FakeFinishedProcess(stdout=payload, returncode=EXIT_CODE_PROVIDER_INACCESSIBLE)

    service = claude_auth.ClaudeAuthService(command_runner=_runner)
    assert service.list_claude_agent_names() == ["ababa", "feature-x"]


def test_restart_all_claude_agents_stops_all_then_starts_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every agent is stopped before any is started.

    The stop-all/start-all ordering is required so the Claude config
    prepared between the two phases isn't clobbered by a still-running
    agent's stale in-memory copy.
    """
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    payload = '{"agents": [{"name": "a", "type": "claude"}, {"name": "b", "type": "claude"}]}'
    calls: list[list[str]] = []

    def _runner(cmd: list[str], _timeout: float) -> FakeFinishedProcess:
        if cmd[:3] == ["mngr", "list", "--format"]:
            return FakeFinishedProcess(stdout=payload)
        if cmd[0] == "mngr" and cmd[1] in {"stop", "start"}:
            calls.append(cmd)
            return FakeFinishedProcess(returncode=0)
        raise AssertionError(f"unexpected cmd: {cmd!r}")

    service = claude_auth.ClaudeAuthService(command_runner=_runner)
    names = service.restart_all_claude_agents()
    assert names == ["a", "b"]
    # The agent name is always the last arg of each mngr stop/start call.
    assert [f"{cmd[1]} {cmd[-1]}" for cmd in calls] == ["stop a", "stop b", "start a", "start b"]
    # Every start passes --no-resume so mngr does not send the resume message.
    assert all("--no-resume" in cmd for cmd in calls if cmd[1] == "start")


def test_restart_all_claude_agents_prepares_config_between_stop_and_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Config prep runs after all stops and before any start, with the key approved."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    payload = '{"agents": [{"name": "a", "type": "claude"}]}'
    calls: list[list[str]] = []

    def _runner(cmd: list[str], _timeout: float) -> FakeFinishedProcess:
        if cmd[:3] == ["mngr", "list", "--format"]:
            return FakeFinishedProcess(stdout=payload)
        if cmd[0] == "mngr" and cmd[1] in {"stop", "start"}:
            calls.append(cmd)
            return FakeFinishedProcess(returncode=0)
        raise AssertionError(f"unexpected cmd: {cmd!r}")

    service = claude_auth.ClaudeAuthService(command_runner=_runner)
    service.restart_all_claude_agents(api_key=SecretStr("sk-ant-key-abcdefghijklmnop1234"))

    assert [f"{cmd[1]} {cmd[-1]}" for cmd in calls] == ["stop a", "start a"]
    config = json.loads((tmp_path / ".claude.json").read_text())
    assert config["hasCompletedOnboarding"] is True
    assert config["customApiKeyResponses"]["approved"] == ["abcdefghijklmnop1234"]


def test_prepare_claude_config_dismisses_dialogs_and_approves_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    claude_auth._prepare_claude_config_for_restart(SecretStr("sk-ant-x" + "y" * 24))
    config = json.loads((tmp_path / ".claude.json").read_text())
    assert config["hasCompletedOnboarding"] is True
    assert config["effortCalloutDismissed"] is True
    assert config["hasAcknowledgedCostThreshold"] is True
    assert config["customApiKeyResponses"]["approved"] == ["y" * 20]
    assert config["customApiKeyResponses"]["rejected"] == []


def test_prepare_claude_config_skips_key_approval_when_no_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    claude_auth._prepare_claude_config_for_restart(None)
    config = json.loads((tmp_path / ".claude.json").read_text())
    assert config["hasCompletedOnboarding"] is True
    assert "customApiKeyResponses" not in config


def test_approve_api_key_preserves_existing_approved_entries(tmp_path: Path) -> None:
    config_path = tmp_path / ".claude.json"
    config_path.write_text('{"customApiKeyResponses": {"approved": ["existing-suffix-000"]}}')
    claude_auth._approve_api_key_in_claude_config(config_path, SecretStr("sk-ant-" + "z" * 30))
    config = json.loads(config_path.read_text())
    assert config["customApiKeyResponses"]["approved"] == ["existing-suffix-000", "z" * 20]


def test_resolve_claude_config_path_raises_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    with pytest.raises(claude_auth.ClaudeAuthError, match="CLAUDE_CONFIG_DIR"):
        claude_auth._resolve_claude_config_path()


def test_submit_oauth_code_drives_subprocess_and_returns_status() -> None:
    fake_url = "https://claude.ai/oauth/authorize?x=1"
    fake_process = FakePexpectProcess(url_match=fake_url, expect_return_index=0)
    service = claude_auth.ClaudeAuthService(
        command_runner=lambda _cmd, _timeout: FakeFinishedProcess(stdout='{"loggedIn": true, "email": "x@y.com"}'),
        pexpect_spawner=lambda *_args, **_kwargs: fake_process,
    )
    start = service.start_oauth_login(claude_auth.OAuthProvider.CLAUDEAI)
    status = service.submit_oauth_code(start.session_id, "CODE#STATE")
    assert status.logged_in is True
    assert status.email == "x@y.com"
    assert fake_process.sendline_calls == ["CODE#STATE"]


def test_submit_oauth_code_claudeai_does_not_restart_agents() -> None:
    """The subscription provider's credential is re-read live -- no restart."""
    fake_url = "https://claude.ai/oauth/authorize?x=1"
    fake_process = FakePexpectProcess(url_match=fake_url, expect_return_index=0)
    commands: list[tuple[str, ...]] = []

    def _runner(cmd: list[str], _timeout: float) -> FakeFinishedProcess:
        commands.append(tuple(cmd))
        return FakeFinishedProcess(stdout='{"loggedIn": true}')

    service = claude_auth.ClaudeAuthService(command_runner=_runner, pexpect_spawner=lambda *_a, **_k: fake_process)
    start = service.start_oauth_login(claude_auth.OAuthProvider.CLAUDEAI)
    service.submit_oauth_code(start.session_id, "CODE#STATE")
    assert all(cmd[:2] != ("mngr", "stop") for cmd in commands)
    assert all(cmd[:2] != ("mngr", "start") for cmd in commands)


def test_submit_oauth_code_console_restarts_agents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The console provider writes primaryApiKey into the cached .claude.json,
    so every claude agent must be restarted to pick it up."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    fake_url = "https://platform.claude.com/oauth/authorize?x=1"
    fake_process = FakePexpectProcess(url_match=fake_url, expect_return_index=0)
    restart_calls: list[list[str]] = []
    list_payload = '{"agents": [{"name": "chat", "type": "claude"}]}'

    def _runner(cmd: list[str], _timeout: float) -> FakeFinishedProcess:
        if cmd[:3] == ["mngr", "list", "--format"]:
            return FakeFinishedProcess(stdout=list_payload)
        if cmd[0] == "mngr" and cmd[1] in {"stop", "start"}:
            restart_calls.append(cmd)
            return FakeFinishedProcess(returncode=0)
        return FakeFinishedProcess(stdout='{"loggedIn": true}')

    service = claude_auth.ClaudeAuthService(command_runner=_runner, pexpect_spawner=lambda *_a, **_k: fake_process)
    start = service.start_oauth_login(claude_auth.OAuthProvider.CONSOLE)
    status = service.submit_oauth_code(start.session_id, "CODE#STATE")
    assert status.logged_in is True
    assert [f"{cmd[1]} {cmd[-1]}" for cmd in restart_calls] == ["stop chat", "start chat"]
    assert all("--no-resume" in cmd for cmd in restart_calls if cmd[1] == "start")


def test_get_auth_status_overlays_extra_env_onto_status_subprocess() -> None:
    """`extra_env` is passed through to the status subprocess environment."""
    seen_env: dict[str, Mapping[str, str] | None] = {}

    def _runner(_cmd: list[str], _timeout: float, env: Mapping[str, str] | None = None) -> FakeFinishedProcess:
        seen_env["env"] = env
        return FakeFinishedProcess(stdout='{"loggedIn": true}')

    service = claude_auth.ClaudeAuthService(command_runner=_runner)
    status = service.get_auth_status(extra_env={"ANTHROPIC_API_KEY": "sk-ant-probe"})
    assert status.logged_in is True
    passed = seen_env["env"]
    assert passed is not None
    assert passed["ANTHROPIC_API_KEY"] == "sk-ant-probe"


def test_submit_api_key_verifies_status_with_key_in_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The verification `claude auth status` runs with the key in its env.

    Regression guard: the system-interface process never receives the key
    written to the host env file, so a status check that doesn't overlay
    the key would report `loggedIn=false` for a valid key and the modal
    would wrongly tell the user it was rejected.
    """
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    the_key = "sk-ant-valid-key-abcdefghijklmnop"

    def _runner(cmd: list[str], _timeout: float, env: Mapping[str, str] | None = None) -> FakeFinishedProcess:
        if cmd[:3] == ["mngr", "list", "--format"]:
            return FakeFinishedProcess(stdout='{"agents": []}')
        if cmd[:3] == ["claude", "auth", "status"]:
            # Model claude's real behavior: logged in iff the key is in env.
            logged_in = bool(env is not None and env.get("ANTHROPIC_API_KEY"))
            return FakeFinishedProcess(stdout=json.dumps({"loggedIn": logged_in}))
        return FakeFinishedProcess(returncode=0)

    service = claude_auth.ClaudeAuthService(command_runner=_runner)
    status = service.submit_api_key(SecretStr(the_key))
    assert status.logged_in is True


# --- mngr CLI argv contract ---
# Confront each builder's argv with the live ``imbue.mngr.main.cli`` tree, so a
# vendor/mngr subcommand/flag rename to list/stop/start fails here at merge time
# rather than only surfacing at runtime.


def test_list_argv_accepted_by_live_cli() -> None:
    assert_mngr_argv_valid(claude_auth._build_list_command())


def test_stop_argv_accepted_by_live_cli() -> None:
    assert_mngr_argv_valid(claude_auth._build_stop_command("demo"))


def test_start_argv_accepted_by_live_cli() -> None:
    assert_mngr_argv_valid(claude_auth._build_start_command("demo"))
