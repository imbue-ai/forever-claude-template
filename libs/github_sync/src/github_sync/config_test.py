"""Unit tests for github_sync config parsing and gateway env helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from github_sync.config import (
    GithubSyncConfigError,
    get_gateway_password,
    get_gateway_url,
    get_secondary_gateway_url,
    load_repo_url,
    parse_owner_and_name,
    proxied_url,
)


def test_load_repo_url_returns_none_when_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert load_repo_url() is None


def test_load_repo_url_normalizes_git_suffix_and_trailing_slash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "github_sync.toml").write_text(
        'repo_url = "https://github.com/some-user/my-workspace.git"\n'
    )
    assert load_repo_url() == "https://github.com/some-user/my-workspace"


def test_load_repo_url_raises_on_malformed_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "github_sync.toml").write_text("repo_url = [not toml")
    with pytest.raises(GithubSyncConfigError):
        load_repo_url()


def test_load_repo_url_raises_on_non_github_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "github_sync.toml").write_text(
        'repo_url = "https://gitlab.com/user/repo"\n'
    )
    with pytest.raises(GithubSyncConfigError):
        load_repo_url()


def test_load_repo_url_raises_when_repo_url_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "github_sync.toml").write_text('other_key = "value"\n')
    with pytest.raises(GithubSyncConfigError):
        load_repo_url()


def test_parse_owner_and_name_splits_url() -> None:
    assert parse_owner_and_name("https://github.com/some-user/my-repo") == (
        "some-user",
        "my-repo",
    )
    assert parse_owner_and_name("https://github.com/some-user/my-repo.git") == (
        "some-user",
        "my-repo",
    )


def test_parse_owner_and_name_rejects_malformed_url() -> None:
    with pytest.raises(GithubSyncConfigError):
        parse_owner_and_name("https://github.com/just-an-owner")
    with pytest.raises(GithubSyncConfigError):
        parse_owner_and_name("https://github.com/a/b/c")


def test_proxied_url_composes_gateway_git_proxy() -> None:
    assert (
        proxied_url("http://127.0.0.1:39000", "https://github.com/u/r.git")
        == "http://127.0.0.1:39000/gateway/https://github.com/u/r.git"
    )


def test_gateway_env_getters_strip_and_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LATCHKEY_GATEWAY", "http://127.0.0.1:39000/")
    assert get_gateway_url() == "http://127.0.0.1:39000"
    monkeypatch.delenv("LATCHKEY_GATEWAY", raising=False)
    assert get_gateway_url() is None
    monkeypatch.setenv("LATCHKEY_GATEWAY_SECONDARY", "")
    assert get_secondary_gateway_url() is None
    monkeypatch.delenv("LATCHKEY_GATEWAY_PASSWORD", raising=False)
    assert get_gateway_password() is None
    monkeypatch.setenv("LATCHKEY_GATEWAY_PASSWORD", "pw-123")
    assert get_gateway_password() == "pw-123"
