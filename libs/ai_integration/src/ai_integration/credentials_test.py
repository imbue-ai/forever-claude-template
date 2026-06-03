import pytest

from ai_integration.credentials import (
    MAIN_CLAUDE_SESSION_ID,
    build_claude_cli_env,
    get_api_key,
    has_resolvable_credentials,
    require_credentials,
)
from ai_integration.errors import CredentialsUnavailableError


def test_build_claude_cli_env_unsets_main_session_id() -> None:
    env = {"PATH": "/usr/bin", MAIN_CLAUDE_SESSION_ID: "sess-123", "HOME": "/home/x"}
    child = build_claude_cli_env(env)
    assert MAIN_CLAUDE_SESSION_ID not in child
    assert child["PATH"] == "/usr/bin"


def test_build_claude_cli_env_optionally_strips_mngr_vars() -> None:
    env = {
        MAIN_CLAUDE_SESSION_ID: "s",
        "MNGR_AGENT_STATE_DIR": "/d",
        "MNGR_AGENT_NAME": "a",
        "MNGR_HOST_DIR": "/h",
        "KEEP": "1",
    }
    child = build_claude_cli_env(env, strip_mngr_agent_vars=True)
    assert "MNGR_AGENT_STATE_DIR" not in child
    assert "MNGR_AGENT_NAME" not in child
    assert "MNGR_HOST_DIR" not in child
    assert child["KEEP"] == "1"


def test_build_claude_cli_env_keeps_mngr_vars_by_default() -> None:
    env = {MAIN_CLAUDE_SESSION_ID: "s", "MNGR_AGENT_STATE_DIR": "/d"}
    child = build_claude_cli_env(env)
    assert child["MNGR_AGENT_STATE_DIR"] == "/d"


def test_get_api_key() -> None:
    assert get_api_key({"ANTHROPIC_API_KEY": "sk"}) == "sk"
    assert get_api_key({"ANTHROPIC_API_KEY": ""}) is None
    assert get_api_key({}) is None


def test_has_resolvable_credentials_via_api_key() -> None:
    assert has_resolvable_credentials({"ANTHROPIC_API_KEY": "sk", "HOME": "/none"})


def test_has_resolvable_credentials_via_config_dir(tmp_path) -> None:
    (tmp_path / ".credentials.json").write_text("{}")
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path), "HOME": str(tmp_path / "home")}
    assert has_resolvable_credentials(env)


def test_require_credentials_raises_when_none(tmp_path) -> None:
    # Isolated HOME with no key and no credential files.
    env = {"HOME": str(tmp_path)}
    with pytest.raises(CredentialsUnavailableError):
        require_credentials(env)
