"""Unit tests for the latchkey-gateway git config wiring."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from github_sync.wiring import HOOKS_PATH, apply_git_wiring, remove_git_wiring


def _get_all_global(key: str) -> list[str]:
    result = subprocess.run(
        ["git", "config", "--global", "--get-all", key],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.splitlines()


def _set_gateway_env(monkeypatch: pytest.MonkeyPatch, gateway_url: str) -> None:
    monkeypatch.setenv("LATCHKEY_GATEWAY", gateway_url)
    monkeypatch.setenv("LATCHKEY_GATEWAY_PASSWORD", "pw-abc")
    monkeypatch.setenv("LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE", "jwt-xyz")


def test_apply_git_wiring_writes_rewrite_headers_and_hookspath(
    isolated_git_and_gateway_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_gateway_env(monkeypatch, "http://127.0.0.1:41234")

    assert apply_git_wiring() is True

    insteadof_key = "url.http://127.0.0.1:41234/gateway/https://github.com/.insteadOf"
    assert _get_all_global(insteadof_key) == ["https://github.com/"]
    headers = _get_all_global("http.http://127.0.0.1:41234/.extraHeader")
    assert headers == [
        "X-Latchkey-Gateway-Password: pw-abc",
        "X-Latchkey-Gateway-Permissions-Override: jwt-xyz",
    ]
    assert _get_all_global("core.hooksPath") == [HOOKS_PATH]


def test_apply_git_wiring_fails_without_gateway_env(
    isolated_git_and_gateway_env: Path,
) -> None:
    assert apply_git_wiring() is False
    assert _get_all_global("core.hooksPath") == []


def test_apply_git_wiring_replaces_stale_gateway_entries(
    isolated_git_and_gateway_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gateway URL embeds a port that changes across restarts; re-wiring must
    remove the previous port's entries or git would see two ambiguous
    rewrites for the same prefix."""
    _set_gateway_env(monkeypatch, "http://127.0.0.1:41234")
    assert apply_git_wiring() is True
    _set_gateway_env(monkeypatch, "http://127.0.0.1:59999")

    assert apply_git_wiring() is True

    stale_key = "url.http://127.0.0.1:41234/gateway/https://github.com/.insteadOf"
    fresh_key = "url.http://127.0.0.1:59999/gateway/https://github.com/.insteadOf"
    assert _get_all_global(stale_key) == []
    assert _get_all_global(fresh_key) == ["https://github.com/"]
    assert _get_all_global("http.http://127.0.0.1:41234/.extraHeader") == []
    assert len(_get_all_global("http.http://127.0.0.1:59999/.extraHeader")) == 2


def test_remove_git_wiring_clears_everything_but_keeps_ssh_rewrites(
    isolated_git_and_gateway_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The bootstrap-owned ssh->https rewrite shares the value shape but is not
    # gateway wiring and must survive a disable.
    subprocess.run(
        [
            "git",
            "config",
            "--global",
            "url.https://github.com/.insteadOf",
            "git@github.com:",
        ],
        check=True,
        capture_output=True,
    )
    _set_gateway_env(monkeypatch, "http://127.0.0.1:41234")
    assert apply_git_wiring() is True

    remove_git_wiring()

    gateway_key = "url.http://127.0.0.1:41234/gateway/https://github.com/.insteadOf"
    assert _get_all_global(gateway_key) == []
    assert _get_all_global("http.http://127.0.0.1:41234/.extraHeader") == []
    assert _get_all_global("core.hooksPath") == []
    assert _get_all_global("url.https://github.com/.insteadOf") == ["git@github.com:"]
