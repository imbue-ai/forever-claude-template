"""Unit tests for the gitleaks portion of scripts/deferred_install.sh.

The script is bash, so each test sources it in a fresh bash process (the
script only runs `main` when executed directly) and exercises one
`_install_gitleaks`-related function with test-controlled overrides:

- DEFERRED_INSTALL_MARKER_DIR / GITLEAKS_INSTALL_DIR point into tmp_path;
- a stub-bin dir is prepended to PATH with a fake `curl` (serves a prepared
  tarball and logs its invocation) and a fake `uname` (fixed architecture),
  so no network access and no dependence on the host machine's arch.

The pinned sha256s stay authoritative: the checksum-mismatch test proves a
tarball that doesn't match the pin is rejected, and the success path is
tested via `_fetch_verify_install_gitleaks` with the sha of a test tarball.
"""

from __future__ import annotations

import hashlib
import stat
import subprocess
import tarfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SCRIPT = _REPO_ROOT / "scripts" / "deferred_install.sh"

_FAKE_BINARY_CONTENT = b"#!/bin/sh\necho fake-gitleaks\n"


def _make_gitleaks_tarball(path: Path) -> None:
    """Write a gzipped tarball containing a single `gitleaks` member."""
    binary = path.parent / "gitleaks"
    binary.write_bytes(_FAKE_BINARY_CONTENT)
    with tarfile.open(path, "w:gz") as tar:
        tar.add(binary, arcname="gitleaks")
    binary.unlink()


def _make_stub_bin(stub_bin: Path, *, served_tarball: Path | None, arch: str) -> Path:
    """Create stub `curl` and `uname` executables; return the curl call log path."""
    stub_bin.mkdir(parents=True, exist_ok=True)
    curl_log = stub_bin / "curl_calls.log"
    if served_tarball is None:
        # No tarball prepared: any curl call fails (and is still logged).
        curl_body = f'#!/bin/bash\necho "$@" >> "{curl_log}"\nexit 22\n'
    else:
        curl_body = (
            "#!/bin/bash\n"
            f'echo "$@" >> "{curl_log}"\n'
            'out=""\n'
            'while [ "$#" -gt 0 ]; do\n'
            '    if [ "$1" = "-o" ]; then out="$2"; shift 2; else shift; fi\n'
            "done\n"
            f'cp "{served_tarball}" "$out"\n'
        )
    (stub_bin / "curl").write_text(curl_body)
    (stub_bin / "uname").write_text(f'#!/bin/bash\necho "{arch}"\n')
    for name in ("curl", "uname"):
        exe = stub_bin / name
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return curl_log


def _run_sourced(
    snippet: str, *, extra_env: dict[str, str], stub_bin: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Source deferred_install.sh in a fresh bash and run `snippet`."""
    path_prefix = f"{stub_bin}:" if stub_bin is not None else ""
    script = f'source "{_SCRIPT}"\n{snippet}\n'
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": f"{path_prefix}/usr/bin:/bin:/usr/sbin:/sbin",
            **extra_env,
        },
    )


# --- _gitleaks_asset_for_arch ---


def test_gitleaks_asset_for_arch_maps_x86_64_and_aarch64_to_pinned_assets(
    tmp_path: Path,
) -> None:
    result = _run_sourced(
        "_gitleaks_asset_for_arch x86_64; _gitleaks_asset_for_arch aarch64; _gitleaks_asset_for_arch arm64",
        extra_env={"DEFERRED_INSTALL_MARKER_DIR": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == 3
    x64_asset, x64_sha = lines[0].split()
    arm64_asset, arm64_sha = lines[1].split()
    assert x64_asset == "gitleaks_8.30.1_linux_x64.tar.gz"
    assert arm64_asset == "gitleaks_8.30.1_linux_arm64.tar.gz"
    # arm64 (docker on Apple Silicon reports either) maps to the same asset.
    assert lines[2] == lines[1]
    # The pins are real sha256 hex digests and differ per architecture.
    for sha in (x64_sha, arm64_sha):
        assert len(sha) == 64
        assert all(c in "0123456789abcdef" for c in sha)
    assert x64_sha != arm64_sha


def test_gitleaks_asset_for_arch_rejects_unknown_arch(tmp_path: Path) -> None:
    result = _run_sourced(
        "if _gitleaks_asset_for_arch mips64; then exit 7; fi",
        extra_env={"DEFERRED_INSTALL_MARKER_DIR": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


# --- _fetch_verify_install_gitleaks ---


def test_fetch_verify_install_installs_binary_when_checksum_matches(
    tmp_path: Path,
) -> None:
    tarball = tmp_path / "gitleaks.tar.gz"
    _make_gitleaks_tarball(tarball)
    sha = hashlib.sha256(tarball.read_bytes()).hexdigest()
    stub_bin = tmp_path / "stub-bin"
    _make_stub_bin(stub_bin, served_tarball=tarball, arch="x86_64")
    dest = tmp_path / "install" / "gitleaks"

    result = _run_sourced(
        f'_fetch_verify_install_gitleaks "https://fake.invalid/gitleaks.tar.gz" "{sha}" "{dest}"',
        extra_env={"DEFERRED_INSTALL_MARKER_DIR": str(tmp_path / "markers")},
        stub_bin=stub_bin,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert dest.read_bytes() == _FAKE_BINARY_CONTENT
    assert dest.stat().st_mode & stat.S_IXUSR


def test_fetch_verify_install_rejects_checksum_mismatch(tmp_path: Path) -> None:
    tarball = tmp_path / "gitleaks.tar.gz"
    _make_gitleaks_tarball(tarball)
    wrong_sha = "0" * 64
    stub_bin = tmp_path / "stub-bin"
    _make_stub_bin(stub_bin, served_tarball=tarball, arch="x86_64")
    dest = tmp_path / "install" / "gitleaks"

    result = _run_sourced(
        f'_fetch_verify_install_gitleaks "https://fake.invalid/gitleaks.tar.gz" "{wrong_sha}" "{dest}"',
        extra_env={"DEFERRED_INSTALL_MARKER_DIR": str(tmp_path / "markers")},
        stub_bin=stub_bin,
    )
    assert result.returncode != 0
    assert "sha256 MISMATCH" in result.stdout
    assert not dest.exists()


# --- _install_gitleaks ---


def test_install_gitleaks_skips_when_marker_present(tmp_path: Path) -> None:
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    (marker_dir / "done.gitleaks").write_text("")
    stub_bin = tmp_path / "stub-bin"
    curl_log = _make_stub_bin(stub_bin, served_tarball=None, arch="x86_64")
    install_dir = tmp_path / "install"

    result = _run_sourced(
        "_install_gitleaks",
        extra_env={
            "DEFERRED_INSTALL_MARKER_DIR": str(marker_dir),
            "GITLEAKS_INSTALL_DIR": str(install_dir),
        },
        stub_bin=stub_bin,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "skipping" in result.stdout
    assert not curl_log.exists()  # never even attempted a download
    assert not (install_dir / "gitleaks").exists()


def test_install_gitleaks_fails_without_marker_on_checksum_mismatch(
    tmp_path: Path,
) -> None:
    # The stub curl serves a tarball that cannot match the pinned sha256, so
    # the install must fail, install nothing, and leave the marker unwritten
    # (the next boot retries).
    tarball = tmp_path / "gitleaks.tar.gz"
    _make_gitleaks_tarball(tarball)
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    stub_bin = tmp_path / "stub-bin"
    curl_log = _make_stub_bin(stub_bin, served_tarball=tarball, arch="x86_64")
    install_dir = tmp_path / "install"

    result = _run_sourced(
        "_install_gitleaks",
        extra_env={
            "DEFERRED_INSTALL_MARKER_DIR": str(marker_dir),
            "GITLEAKS_INSTALL_DIR": str(install_dir),
        },
        stub_bin=stub_bin,
    )
    assert result.returncode != 0
    assert "sha256 MISMATCH" in result.stdout
    assert "marker not written" in result.stdout
    assert curl_log.exists()  # the download itself did happen
    assert not (marker_dir / "done.gitleaks").exists()
    assert not (install_dir / "gitleaks").exists()


def test_install_gitleaks_fails_without_marker_on_unsupported_arch(
    tmp_path: Path,
) -> None:
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    stub_bin = tmp_path / "stub-bin"
    curl_log = _make_stub_bin(stub_bin, served_tarball=None, arch="mips64")
    install_dir = tmp_path / "install"

    result = _run_sourced(
        "_install_gitleaks",
        extra_env={
            "DEFERRED_INSTALL_MARKER_DIR": str(marker_dir),
            "GITLEAKS_INSTALL_DIR": str(install_dir),
        },
        stub_bin=stub_bin,
    )
    assert result.returncode != 0
    assert "no pinned binary for architecture 'mips64'" in result.stdout
    assert not curl_log.exists()
    assert not (marker_dir / "done.gitleaks").exists()


def test_install_gitleaks_downloads_pinned_release_url(tmp_path: Path) -> None:
    # Even though the checksum then fails (fake tarball), the requested URL
    # must be the pinned release asset for the stubbed architecture.
    tarball = tmp_path / "gitleaks.tar.gz"
    _make_gitleaks_tarball(tarball)
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    stub_bin = tmp_path / "stub-bin"
    curl_log = _make_stub_bin(stub_bin, served_tarball=tarball, arch="aarch64")

    _run_sourced(
        "_install_gitleaks",
        extra_env={
            "DEFERRED_INSTALL_MARKER_DIR": str(marker_dir),
            "GITLEAKS_INSTALL_DIR": str(tmp_path / "install"),
        },
        stub_bin=stub_bin,
    )
    assert (
        "https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/"
        "gitleaks_8.30.1_linux_arm64.tar.gz" in curl_log.read_text()
    )
