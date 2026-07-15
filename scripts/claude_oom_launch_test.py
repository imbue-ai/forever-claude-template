"""Tests for the agent launch wrapper.

The wrapper sets its own memory-shedding band and records its pid, then execs the
real claude with the args mngr appended. We verify the band classification
directly, and the tag+exec+arg-forwarding end to end via a subprocess with a fake
``claude`` on PATH (so the real ``execvp`` runs without launching Claude Code).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent / "claude_oom_launch.py"
_spec = importlib.util.spec_from_file_location("claude_oom_launch", _SCRIPT)
assert _spec is not None and _spec.loader is not None
wrapper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wrapper)

from oom_priority.registry import lookup_agent


def _write_agent_record(
    host_dir: Path, name: str, *, is_worker: bool, labels: dict | None = None
) -> None:
    """Seed the host agent record the identity checks read to classify ``name``."""
    agent_dir = host_dir / "agents" / "id"
    agent_dir.mkdir(parents=True)
    resolved = (
        labels
        if labels is not None
        else ({"agent_created": "true"} if is_worker else {"user_created": "true"})
    )
    (agent_dir / "data.json").write_text(json.dumps({"name": name, "labels": resolved}))


def test_tag_self_classifies_worker_into_the_worker_band(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker (``agent_created`` label) records the worker band; the registry
    entry maps this process's pid back to it as a worker."""
    monkeypatch.setenv("OOM_PRIORITY_RUNTIME_DIR", str(tmp_path / "rt"))
    host = tmp_path / "host"
    _write_agent_record(host, "w1", is_worker=True)
    monkeypatch.setenv("MNGR_HOST_DIR", str(host))
    monkeypatch.setenv("MNGR_AGENT_NAME", "w1")

    wrapper._tag_self()

    entry = lookup_agent(os.getpid())
    assert entry is not None
    assert entry["agent_name"] == "w1"
    assert entry["is_worker"] is True


def test_tag_self_records_a_chat_as_non_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chat (no ``agent_created`` label) is recorded as a non-worker so the
    kill hook can tell an agent shed from a subprocess shed."""
    monkeypatch.setenv("OOM_PRIORITY_RUNTIME_DIR", str(tmp_path / "rt"))
    host = tmp_path / "host"
    _write_agent_record(host, "u1", is_worker=False)
    monkeypatch.setenv("MNGR_HOST_DIR", str(host))
    monkeypatch.setenv("MNGR_AGENT_NAME", "u1")

    wrapper._tag_self()

    entry = lookup_agent(os.getpid())
    assert entry is not None
    assert entry["is_worker"] is False


def test_band_for_pins_the_primary_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The primary (services) agent is pinned to the never-shed PRIMARY_AGENT band,
    ahead of the worker/user classification."""
    host = tmp_path / "host"
    _write_agent_record(
        host,
        "services",
        is_worker=False,
        labels={"is_primary": "true", "user_created": "true"},
    )
    monkeypatch.setenv("MNGR_HOST_DIR", str(host))
    # is_primary wins even though the record also carries user_created.
    assert wrapper._band_for("services") == wrapper.bands.PRIMARY_AGENT


def test_band_for_starts_a_chat_expendable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chat (user_created) launches at the most-expendable chat band, to be
    protected later by live UI engagement -- not at the protected floor."""
    host = tmp_path / "host"
    _write_agent_record(host, "c1", is_worker=False, labels={"user_created": "true"})
    monkeypatch.setenv("MNGR_HOST_DIR", str(host))
    assert wrapper._band_for("c1") == wrapper.bands.CHAT_AGENT_BASE


def test_band_for_puts_workers_and_unidentifiable_agents_in_the_worker_band(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker, and an agent whose record cannot be read to classify it, both
    land at the least-protected agent tier -- we must not shield an agent we
    cannot identify."""
    host = tmp_path / "host"
    _write_agent_record(host, "w1", is_worker=True, labels={"agent_created": "true"})
    monkeypatch.setenv("MNGR_HOST_DIR", str(host))
    assert wrapper._band_for("w1") == wrapper.bands.WORKER_AGENT
    # No record for "ghost" -> unclassifiable -> least protected.
    assert wrapper._band_for("ghost") == wrapper.bands.WORKER_AGENT


def test_tag_self_records_the_agent_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stable agent id is recorded so the prioritizer can resolve the pid by id."""
    monkeypatch.setenv("OOM_PRIORITY_RUNTIME_DIR", str(tmp_path / "rt"))
    host = tmp_path / "host"
    _write_agent_record(host, "u1", is_worker=False)
    monkeypatch.setenv("MNGR_HOST_DIR", str(host))
    monkeypatch.setenv("MNGR_AGENT_NAME", "u1")
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-xyz")

    wrapper._tag_self()

    entry = lookup_agent(os.getpid())
    assert entry is not None
    assert entry["agent_id"] == "agent-xyz"


def test_tag_self_noops_without_agent_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``MNGR_AGENT_NAME`` -> nothing is recorded (the band check is skipped)."""
    monkeypatch.setenv("OOM_PRIORITY_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.delenv("MNGR_AGENT_NAME", raising=False)

    wrapper._tag_self()

    assert lookup_agent(os.getpid()) is None


def _fake_claude_dir(tmp_path: Path, args_out: Path) -> Path:
    """A directory holding a fake ``claude`` that records the args it was exec'd
    with, so the wrapper's real ``execvp`` can be observed without Claude Code."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "claude"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > ' + str(args_out) + "\n")
    fake.chmod(0o755)
    return bindir


def test_wrapper_execs_claude_forwarding_args_after_tagging(
    tmp_path: Path,
) -> None:
    """End to end: the wrapper records its pid, then execs claude with exactly the
    args it was given (the flags mngr splices after the command base)."""
    args_out = tmp_path / "claude_args.txt"
    bindir = _fake_claude_dir(tmp_path, args_out)
    runtime = tmp_path / "rt"
    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "OOM_PRIORITY_RUNTIME_DIR": str(runtime),
        "MNGR_AGENT_NAME": "u1",
    }
    env.pop("MNGR_HOST_DIR", None)  # no host records -> classified as a user agent

    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--settings", "foo", "--resume", "bar"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert args_out.read_text().splitlines() == ["--settings", "foo", "--resume", "bar"]
    # The wrapper recorded its own pid (which became claude's) as agent u1.
    # OOM_PRIORITY_RUNTIME_DIR is the runtime dir itself (the override is used
    # verbatim), so the registry lives directly under it.
    pid_files = list((runtime / "agent_pids").glob("*.json"))
    assert len(pid_files) == 1
    assert json.loads(pid_files[0].read_text())["agent_name"] == "u1"


def test_wrapper_still_execs_claude_when_tagging_fails(tmp_path: Path) -> None:
    """A tagging failure must never block the agent: even when the registry path
    is unwritable, the wrapper still execs claude with its args."""
    args_out = tmp_path / "claude_args.txt"
    bindir = _fake_claude_dir(tmp_path, args_out)
    # Point the runtime dir under a regular file so the registry mkdir raises.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "OOM_PRIORITY_RUNTIME_DIR": str(blocker / "rt"),
        "MNGR_AGENT_NAME": "u1",
    }
    env.pop("MNGR_HOST_DIR", None)

    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--session-id", "abc"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert args_out.read_text().splitlines() == ["--session-id", "abc"]
