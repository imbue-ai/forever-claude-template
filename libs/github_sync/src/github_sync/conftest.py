"""Fixtures shared by the github_sync test files."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def isolated_git_and_gateway_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    """Isolate global git config and clear all latchkey/gateway env vars.

    Keeps tests from ever touching the developer's real ~/.gitconfig (wiring
    writes global config) and from picking up a real gateway from the
    environment. Returns the isolated global gitconfig path.
    """
    gitconfig = tmp_path / "gitconfig"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    for name in (
        "LATCHKEY_GATEWAY",
        "LATCHKEY_GATEWAY_SECONDARY",
        "LATCHKEY_GATEWAY_PASSWORD",
        "LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE",
    ):
        monkeypatch.delenv(name, raising=False)
    # Steer subprocesses away from any inherited status-file override.
    monkeypatch.setenv(
        "GITHUB_SYNC_STATUS_FILE", str(tmp_path / "github-sync-status.json")
    )
    return gitconfig


@pytest.fixture
def fake_latchkey_bin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A directory prepended to PATH where tests install a fake `latchkey`."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return bin_dir
