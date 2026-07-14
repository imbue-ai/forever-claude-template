"""Unit tests for scripts/install_secret_scanners.sh.

The script is the single source of truth for the secret-scanner version pins
and per-arch sha256s. It is bash, so each test sources it in a fresh bash
process and exercises one function with test-controlled overrides (see
bootstrap.testing for the stub `curl`/`uname` mechanics).

The pinned sha256s stay authoritative: the checksum-mismatch tests prove a
tarball that doesn't match the pin is rejected, and the success path is
tested via `_fetch_verify_install` with the sha of a test tarball.
"""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path

from bootstrap.testing import REPO_ROOT
from bootstrap.testing import FAKE_BINARY_CONTENT
from bootstrap.testing import install_fake_pinned_scanner
from bootstrap.testing import make_scanner_tarball
from bootstrap.testing import make_stub_bin
from bootstrap.testing import run_sourced

_SCRIPT = REPO_ROOT / "scripts" / "install_secret_scanners.sh"

_ALL_TOOLS = ("betterleaks", "kingfisher")


# --- _scanner_asset_for_arch / _scanner_release_url ---


def test_scanner_asset_for_arch_maps_x86_64_and_aarch64_per_tool() -> None:
    snippet = "\n".join(
        f"_scanner_asset_for_arch {tool} x86_64; _scanner_asset_for_arch {tool} aarch64; _scanner_asset_for_arch {tool} arm64"
        for tool in _ALL_TOOLS
    )
    result = run_sourced(_SCRIPT, snippet, extra_env={})
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == 3 * len(_ALL_TOOLS)
    for tool_index, tool in enumerate(_ALL_TOOLS):
        x64_asset, x64_sha = lines[tool_index * 3].split()
        arm64_asset, arm64_sha = lines[tool_index * 3 + 1].split()
        # arm64 (docker on Apple Silicon reports either) maps to the same asset.
        assert lines[tool_index * 3 + 2] == lines[tool_index * 3 + 1]
        assert "linux" in x64_asset
        assert "linux" in arm64_asset
        assert x64_asset != arm64_asset
        # The pins are real sha256 hex digests and differ per architecture.
        for sha in (x64_sha, arm64_sha):
            assert len(sha) == 64
            assert all(c in "0123456789abcdef" for c in sha)
        assert x64_sha != arm64_sha


def test_scanner_asset_for_arch_rejects_unknown_arch() -> None:
    result = run_sourced(
        _SCRIPT,
        "if _scanner_asset_for_arch betterleaks mips64; then exit 7; fi",
        extra_env={},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_scanner_release_urls_point_at_pinned_releases() -> None:
    snippet = "\n".join(
        f'asset_and_sha="$(_scanner_asset_for_arch {tool} x86_64)"; _scanner_release_url {tool} "${{asset_and_sha%% *}}"'
        for tool in _ALL_TOOLS
    )
    result = run_sourced(_SCRIPT, snippet, extra_env={})
    assert result.returncode == 0, result.stderr
    urls = result.stdout.splitlines()
    assert urls == [
        "https://github.com/betterleaks/betterleaks/releases/download/v1.6.1/betterleaks_1.6.1_linux_x64.tar.gz",
        "https://github.com/mongodb/kingfisher/releases/download/v1.106.0/kingfisher-linux-x64.tgz",
    ]


# --- _fetch_verify_install ---


def test_fetch_verify_install_installs_binary_when_checksum_matches(
    tmp_path: Path,
) -> None:
    tarball = tmp_path / "betterleaks.tar.gz"
    make_scanner_tarball(tarball, "betterleaks")
    sha = hashlib.sha256(tarball.read_bytes()).hexdigest()
    stub_bin = tmp_path / "stub-bin"
    make_stub_bin(stub_bin, served_tarball=tarball, arch="x86_64")
    dest = tmp_path / "install" / "betterleaks"

    result = run_sourced(
        _SCRIPT,
        f'_fetch_verify_install betterleaks "https://fake.invalid/betterleaks.tar.gz" "{sha}" "{dest}"',
        extra_env={},
        stub_bin=stub_bin,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert dest.read_bytes() == FAKE_BINARY_CONTENT
    assert dest.stat().st_mode & stat.S_IXUSR


def test_fetch_verify_install_rejects_checksum_mismatch(tmp_path: Path) -> None:
    tarball = tmp_path / "betterleaks.tar.gz"
    make_scanner_tarball(tarball, "betterleaks")
    wrong_sha = "0" * 64
    stub_bin = tmp_path / "stub-bin"
    make_stub_bin(stub_bin, served_tarball=tarball, arch="x86_64")
    dest = tmp_path / "install" / "betterleaks"

    result = run_sourced(
        _SCRIPT,
        f'_fetch_verify_install betterleaks "https://fake.invalid/betterleaks.tar.gz" "{wrong_sha}" "{dest}"',
        extra_env={},
        stub_bin=stub_bin,
    )
    assert result.returncode != 0
    assert "sha256 MISMATCH" in result.stdout
    assert not dest.exists()


# --- _install_scanner ---


def test_install_scanner_skips_without_network_when_pinned_version_installed(
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "install"
    install_fake_pinned_scanner(install_dir, _SCRIPT, "kingfisher")
    stub_bin = tmp_path / "stub-bin"
    curl_log = make_stub_bin(stub_bin, served_tarball=None, arch="x86_64")

    result = run_sourced(
        _SCRIPT,
        "_install_scanner kingfisher",
        extra_env={"SECRET_SCANNER_INSTALL_DIR": str(install_dir)},
        stub_bin=stub_bin,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "already installed at pinned version" in result.stdout
    assert not curl_log.exists()  # never even attempted a download


def test_install_scanner_downloads_when_installed_version_is_not_the_pin(
    tmp_path: Path,
) -> None:
    # A binary that reports some other version must NOT be treated as
    # satisfied: the install proceeds (and here fails on the checksum of the
    # fake tarball, proving the download was attempted).
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    stale = install_dir / "kingfisher"
    stale.write_text('#!/bin/sh\necho "kingfisher 0.0.1"\n')
    stale.chmod(stale.stat().st_mode | stat.S_IXUSR)
    tarball = tmp_path / "kingfisher.tar.gz"
    make_scanner_tarball(tarball, "kingfisher")
    stub_bin = tmp_path / "stub-bin"
    curl_log = make_stub_bin(stub_bin, served_tarball=tarball, arch="x86_64")

    result = run_sourced(
        _SCRIPT,
        "_install_scanner kingfisher",
        extra_env={"SECRET_SCANNER_INSTALL_DIR": str(install_dir)},
        stub_bin=stub_bin,
    )
    assert result.returncode != 0
    assert "sha256 MISMATCH" in result.stdout
    assert curl_log.exists()  # the download itself did happen
    # The stale binary is left in place (all-or-nothing install).
    assert stale.read_text().endswith('echo "kingfisher 0.0.1"\n')


def test_install_scanner_fails_on_unsupported_arch(tmp_path: Path) -> None:
    install_dir = tmp_path / "install"
    stub_bin = tmp_path / "stub-bin"
    curl_log = make_stub_bin(stub_bin, served_tarball=None, arch="mips64")

    result = run_sourced(
        _SCRIPT,
        "_install_scanner kingfisher",
        extra_env={"SECRET_SCANNER_INSTALL_DIR": str(install_dir)},
        stub_bin=stub_bin,
    )
    assert result.returncode != 0
    assert "no pinned binary for architecture 'mips64'" in result.stdout
    assert not curl_log.exists()


def test_install_scanner_downloads_pinned_release_url(tmp_path: Path) -> None:
    # Even though the checksum then fails (fake tarball), the requested URL
    # must be the pinned release asset for the stubbed architecture.
    install_dir = tmp_path / "install"
    tarball = tmp_path / "betterleaks.tar.gz"
    make_scanner_tarball(tarball, "betterleaks")
    stub_bin = tmp_path / "stub-bin"
    curl_log = make_stub_bin(stub_bin, served_tarball=tarball, arch="aarch64")

    run_sourced(
        _SCRIPT,
        "_install_scanner betterleaks",
        extra_env={"SECRET_SCANNER_INSTALL_DIR": str(install_dir)},
        stub_bin=stub_bin,
    )
    assert (
        "https://github.com/betterleaks/betterleaks/releases/download/v1.6.1/"
        "betterleaks_1.6.1_linux_arm64.tar.gz" in curl_log.read_text()
    )


# --- main ---


def test_main_installs_both_by_default_and_isolates_failures(
    tmp_path: Path,
) -> None:
    # kingfisher is already installed at its pin; betterleaks is absent and its
    # download serves a tarball that cannot match the pinned sha256. main must
    # still process ALL tools (isolation) and exit non-zero because one failed.
    install_dir = tmp_path / "install"
    install_fake_pinned_scanner(install_dir, _SCRIPT, "kingfisher")
    tarball = tmp_path / "betterleaks.tar.gz"
    make_scanner_tarball(tarball, "betterleaks")
    stub_bin = tmp_path / "stub-bin"
    make_stub_bin(stub_bin, served_tarball=tarball, arch="x86_64")

    result = run_sourced(
        _SCRIPT,
        "main",
        extra_env={"SECRET_SCANNER_INSTALL_DIR": str(install_dir)},
        stub_bin=stub_bin,
    )
    assert result.returncode != 0
    assert "betterleaks: sha256 MISMATCH" in result.stdout.replace(
        "[install-secret-scanners] ", ""
    )
    assert result.stdout.count("already installed at pinned version") == 1
    assert "one or more secret-scanner installs failed" in result.stdout


def test_main_succeeds_when_every_tool_is_at_its_pin(tmp_path: Path) -> None:
    install_dir = tmp_path / "install"
    for tool in _ALL_TOOLS:
        install_fake_pinned_scanner(install_dir, _SCRIPT, tool)
    stub_bin = tmp_path / "stub-bin"
    curl_log = make_stub_bin(stub_bin, served_tarball=None, arch="x86_64")

    result = run_sourced(
        _SCRIPT,
        "main",
        extra_env={"SECRET_SCANNER_INSTALL_DIR": str(install_dir)},
        stub_bin=stub_bin,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stdout.count("already installed at pinned version") == len(_ALL_TOOLS)
    assert not curl_log.exists()


def test_main_rejects_unknown_tool(tmp_path: Path) -> None:
    result = run_sourced(
        _SCRIPT,
        "main betterleaks not-a-scanner",
        extra_env={"SECRET_SCANNER_INSTALL_DIR": str(tmp_path / "install")},
    )
    assert result.returncode == 2
    assert "unknown scanner 'not-a-scanner'" in result.stdout
