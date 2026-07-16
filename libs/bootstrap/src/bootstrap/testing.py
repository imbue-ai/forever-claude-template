"""Shared helpers for the bash installer-script unit tests.

The scripts under test (scripts/install_secret_scanners.sh and
scripts/deferred_install.sh) are bash, so each test sources one in a fresh
bash process (the scripts only run `main` when executed directly) and
exercises individual functions with test-controlled overrides:

- env vars (DEFERRED_INSTALL_MARKER_DIR / SECRET_SCANNER_INSTALL_DIR) point
  into tmp_path;
- a stub-bin dir is prepended to PATH with a fake `curl` (serves a prepared
  tarball and logs its invocation) and a fake `uname` (fixed architecture),
  so tests need no network access and no dependence on the host's arch.
"""

from __future__ import annotations

import stat
import subprocess
import tarfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]

FAKE_BINARY_CONTENT = b"#!/bin/sh\necho fake-scanner\n"


def make_scanner_tarball(path: Path, member: str) -> None:
    """Write a gzipped tarball containing a single binary member named `member`."""
    binary = path.parent / member
    binary.write_bytes(FAKE_BINARY_CONTENT)
    with tarfile.open(path, "w:gz") as tar:
        tar.add(binary, arcname=member)
    binary.unlink()


def make_stub_bin(stub_bin: Path, *, served_tarball: Path | None, arch: str) -> Path:
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


def run_sourced(
    script: Path, snippet: str, *, extra_env: dict[str, str], stub_bin: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Source `script` in a fresh bash and run `snippet`."""
    path_prefix = f"{stub_bin}:" if stub_bin is not None else ""
    full_script = f'source "{script}"\n{snippet}\n'
    return subprocess.run(
        ["bash", "-c", full_script],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": f"{path_prefix}/usr/bin:/bin:/usr/sbin:/sbin",
            **extra_env,
        },
    )


def install_fake_pinned_scanner(install_dir: Path, script: Path, tool: str) -> None:
    """Drop a fake `tool` binary into `install_dir` that reports the pinned version.

    The pinned version is read from the script under test (via
    `_scanner_pinned_version`), so tests never duplicate the pins.
    """
    install_dir.mkdir(parents=True, exist_ok=True)
    result = run_sourced(script, f"_scanner_pinned_version {tool}", extra_env={})
    assert result.returncode == 0, result.stderr
    pinned = result.stdout.strip()
    assert pinned
    fake = install_dir / tool
    fake.write_text(f'#!/bin/sh\necho "{tool} {pinned}"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
