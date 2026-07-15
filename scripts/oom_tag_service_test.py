"""Tests for the service launch wrapper.

The wrapper sets its own memory-shedding band from a named service key, then
execs the service command with its args untouched. We verify the exec + arg
forwarding end to end via a subprocess with a fake command that records what it
was run with, and -- on a host with a writable ``/proc/self/oom_score_adj`` --
that the band is actually applied and survives the exec.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from oom_priority import bands

_SCRIPT = Path(__file__).parent / "oom_tag_service.py"

_PROC_OOM = Path("/proc/self/oom_score_adj")
_HAS_WRITABLE_PROC_OOM = os.access(_PROC_OOM, os.W_OK)


def _fake_command(tmp_path: Path) -> tuple[Path, Path]:
    """A fake service command that records its args and its own
    ``oom_score_adj`` (so we can observe both the exec forwarding and the tag
    that survived it)."""
    out = tmp_path / "recorded.txt"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "fake-service"
    fake.write_text(
        "#!/bin/sh\n"
        'printf "args:%s\\n" "$*" > ' + str(out) + "\n"
        "cat /proc/self/oom_score_adj >> " + str(out) + " 2>/dev/null || true\n"
    )
    fake.chmod(0o755)
    return bindir, out


def _run(script_args: list[str], bindir: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *script_args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_execs_the_command_forwarding_its_args(tmp_path: Path) -> None:
    bindir, out = _fake_command(tmp_path)
    result = _run(["system_interface", "fake-service", "--flag", "value"], bindir)
    assert result.returncode == 0, result.stderr
    assert out.read_text().splitlines()[0] == "args:--flag value"


def test_unknown_service_key_execs_and_defaults_to_the_user_band(
    tmp_path: Path,
) -> None:
    # An unrecognized key must fail expendable (the user-service band), never
    # keep the fully-protected inherited default.
    bindir, out = _fake_command(tmp_path)
    result = _run(["not-a-real-service", "fake-service", "arg"], bindir)
    assert result.returncode == 0, result.stderr
    recorded = out.read_text().splitlines()
    assert recorded[0] == "args:arg"
    assert "unknown service band" in result.stderr
    if _HAS_WRITABLE_PROC_OOM:
        assert recorded[1] == str(bands.USER_SERVICE)


def test_missing_command_exits_nonzero_with_usage(tmp_path: Path) -> None:
    bindir, _ = _fake_command(tmp_path)
    result = _run(["system_interface"], bindir)
    assert result.returncode == 2
    assert "usage:" in result.stderr


def test_applies_the_service_band_and_it_survives_the_exec(tmp_path: Path) -> None:
    if not _HAS_WRITABLE_PROC_OOM:
        # No writable /proc/self/oom_score_adj (e.g. macOS): tagging is a
        # best-effort no-op, so the end-to-end band check does not apply.
        return
    bindir, out = _fake_command(tmp_path)
    result = _run(["cloudflared", "fake-service"], bindir)
    assert result.returncode == 0, result.stderr
    recorded = out.read_text().splitlines()
    assert recorded[1] == str(bands.SERVICE_BANDS["cloudflared"])
