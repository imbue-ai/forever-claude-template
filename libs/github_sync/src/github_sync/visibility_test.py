"""Unit tests for repo-visibility checking (the private-only enforcement)."""

from __future__ import annotations

from pathlib import Path

import pytest

from github_sync.testing import install_fake_latchkey
from github_sync.visibility import (
    VISIBILITY_PRIVATE,
    VISIBILITY_PUBLIC,
    VISIBILITY_UNKNOWN,
    check_repo_visibility,
    parse_visibility_response,
)

_REPO_URL = "https://github.com/some-user/my-workspace"


def test_parse_visibility_response_private() -> None:
    assert parse_visibility_response('{"private": true}') == VISIBILITY_PRIVATE


def test_parse_visibility_response_public() -> None:
    assert parse_visibility_response('{"private": false}') == VISIBILITY_PUBLIC


def test_parse_visibility_response_unknown_on_error_bodies() -> None:
    # A 404 body (deleted repo), non-JSON output, and non-object JSON must all
    # come back UNKNOWN, which blocks pushes.
    assert parse_visibility_response('{"message": "Not Found"}') == VISIBILITY_UNKNOWN
    assert parse_visibility_response("curl: connection refused") == VISIBILITY_UNKNOWN
    assert parse_visibility_response("[1, 2]") == VISIBILITY_UNKNOWN
    assert parse_visibility_response("") == VISIBILITY_UNKNOWN


def test_check_repo_visibility_reads_private_from_latchkey(
    fake_latchkey_bin: Path,
) -> None:
    install_fake_latchkey(fake_latchkey_bin, 'echo \'{"private": true}\'')
    assert check_repo_visibility(_REPO_URL) == VISIBILITY_PRIVATE


def test_check_repo_visibility_detects_public_repo(fake_latchkey_bin: Path) -> None:
    install_fake_latchkey(fake_latchkey_bin, 'echo \'{"private": false}\'')
    assert check_repo_visibility(_REPO_URL) == VISIBILITY_PUBLIC


def test_check_repo_visibility_unknown_when_latchkey_fails(
    fake_latchkey_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Exit code 7 is what latchkey curl produces when the gateway is offline.
    monkeypatch.delenv("LATCHKEY_GATEWAY_SECONDARY", raising=False)
    install_fake_latchkey(fake_latchkey_bin, "exit 7")
    assert check_repo_visibility(_REPO_URL) == VISIBILITY_UNKNOWN


def test_check_repo_visibility_falls_back_to_secondary_gateway(
    fake_latchkey_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The fake gateway only answers when called with the secondary gateway's
    # env override (primary attempt fails), proving the fallback fires with
    # the documented env shape (secondary URL + cleared permissions override).
    monkeypatch.setenv("LATCHKEY_GATEWAY", "http://primary.invalid:1")
    monkeypatch.setenv("LATCHKEY_GATEWAY_SECONDARY", "http://127.0.0.1:46123")
    monkeypatch.setenv("LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE", "some-jwt")
    install_fake_latchkey(
        fake_latchkey_bin,
        'if [ "$LATCHKEY_GATEWAY" = "http://127.0.0.1:46123" ] '
        '&& [ -z "$LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE" ]; then '
        "echo '{\"private\": true}'; else exit 7; fi",
    )
    assert check_repo_visibility(_REPO_URL) == VISIBILITY_PRIVATE
