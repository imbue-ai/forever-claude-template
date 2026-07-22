#!/usr/bin/env python3
"""Spin up an isolated, throwaway instance of a service on a spare port.

This is the shared substrate under two service flows:

- ``update-service`` boots a copy of a service against a *copy* of its data
  (``DATA_DIR`` pointed at a scratch dir) so an edit can be exercised -- writes,
  deletes, migrations -- without ever touching the user's live store. The agent
  reaches the instance directly on its loopback port.
- ``update-system-interface`` boots an already-built work_dir (the lead's editing
  worktree during its live loop, or a worker's work_dir for a final pre-merge
  check) as a *preview* the user clicks around, and ``refresh``-es it in place as
  the lead edits.

Both are the same motion: launch the service on a free port, with environment
overrides that isolate its writable state, wait until it is healthy, and
(optionally) surface it to the user as a labeled "preview" tab. The only thing
that differs is *what* is launched and *how* its state is isolated -- so this
script is deliberately unopinionated and takes all of that as parameters. The
calling skill supplies the specifics.

The two shapes:

- **Bare instance (own testing).** Given just ``--name``, ``--cwd``,
  ``--port-env`` and the launch argv, it picks a free port, injects it into the
  named env var, boots the service, waits for health, and prints the loopback URL
  to stdout. Nothing is registered with the workspace UI; the agent curls /
  drives the port directly. ``down`` kills it.
- **Preview (surface to the user).** Add ``--service-name`` to also register the
  instance as a proxied service, and ``--preview-service-name`` + ``--preview-title``
  to wrap it in a labeled "preview" frame (``preview_wrapper_server.py``) the user
  opens as a tab. ``down`` kills both servers and deregisters both services.

The service must read its port (and, when relevant, its data dir) from the
environment -- that is what ``--port-env`` / ``--env`` inject. Scaffolded Flask
services do this out of the box (``<PKG>_PORT`` / ``<PKG>_DATA_DIR``); an older
service is retrofitted with the same one-liner when it is edited.

Run via bare ``python3`` (standard library only) -- like ``forward_port.py`` and
``reveal_system_interface.py``, it orchestrates the environment, so it must not
depend on any particular venv being synced.

Usage:
    python3 serve_isolated_instance.py up --name <slug> --cwd <dir> \\
        --port-env <ENVVAR> [--host-env <ENVVAR>] \\
        [--env NAME=VALUE ...] [--unset-env NAME ...] [--health-path /path] \\
        [--service-name <name>] \\
        [--preview-service-name <name> --preview-title <label>] \\
        [--repo-root PATH] -- <launch argv...>
    python3 serve_isolated_instance.py refresh --name <slug> [--repo-root PATH]
    python3 serve_isolated_instance.py down --name <slug> [--repo-root PATH]

``refresh`` re-boots the inner server (only) on its existing port so a rebuild or
edit is picked up in place -- the port, the wrapper frame, the service
registrations, and the user's tab all stay put.

Exit codes:
    0  Success (instance is up and healthy / torn down).
    1  Failure to boot (partial state torn down), or a bad argument / unreadable
       state file.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Sequence

# State (detached pids, ports, registered service names) lives under the caller's
# ``runtime/`` so it is gitignored and survives between the separate ``up`` and
# ``down`` invocations. One instance per ``--name``; ``up`` tears down any stale
# instance for the name first.
STATE_ROOT = "runtime/isolated-instances"
STATE_FILENAME = "instance.json"
INNER_LOG_FILENAME = "instance.log"
WRAPPER_LOG_FILENAME = "wrapper.log"

# forward_port.py imports tomlkit (a venv dependency), but this script is run via
# bare python3 with no venv assumed. Invoke it through ``uv run`` (like
# ``reveal_system_interface.py`` does) so the dependency is always resolved.
FORWARD_PORT_CMD = ("uv", "run", "python3", "scripts/forward_port.py")

# The wrapper server ships beside this script and is stdlib-only, so it runs under
# the same bare ``python3`` that runs this script -- no venv resolution.
WRAPPER_SCRIPT = "preview_wrapper_server.py"
_WRAPPER_SCRIPT_PATH = Path(__file__).resolve().parent / WRAPPER_SCRIPT

# Boot budget: a fresh instance (first import + startup) runs alongside whatever
# else is on the box, so give it a generous grace before declaring it dead.
_HEALTH_ATTEMPTS = 60
_HEALTH_INTERVAL_SECONDS = 1.0

# When ``refresh`` reboots the inner server, how long to wait for the old process
# to exit (and release its listening socket) before rebinding the same port.
_STOP_ATTEMPTS = 30
_STOP_INTERVAL_SECONDS = 0.5


class InstanceError(Exception):
    """A throwaway instance failed to boot (avoids raising built-in exceptions)."""


class Runner:
    """Indirection over ``subprocess.run`` so tests can intercept commands."""

    def run(self, argv: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(list(argv), **kwargs)

    def kill_process_group(self, pid: int, sig: int = signal.SIGTERM) -> None:
        """Send ``sig`` to the whole process group led by ``pid``; a no-op if the
        group is already gone.

        Uses ``os.killpg`` directly rather than shelling out to ``kill -<sig>
        -<pid>``: the external procps-ng ``kill`` mis-parses a bare negative-pid
        argument and can signal PID 1 / unrelated groups (procps-ng issue #65),
        which inside a container whose PID 1 traps SIGTERM restarts the whole
        container. ``os.killpg`` targets exactly the intended group.
        """
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            pass

    def process_group_alive(self, pid: int) -> bool:
        """Whether the process group led by ``pid`` still exists.

        Used by ``refresh`` to wait for a just-killed inner server to actually
        exit before rebinding its port: a live listening socket cannot be
        rebound (even with SO_REUSEADDR), so we must not respawn until the old
        process is gone. Signal 0 probes existence without delivering a signal.
        """
        try:
            os.killpg(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # The group exists but is not ours to signal -- still "alive".
            return True


class HttpClient:
    """Indirection over the loopback health probe."""

    def get_status(self, url: str, timeout: float) -> int | None:
        """Return the HTTP status for a GET, or ``None`` if the host is unreachable."""
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return int(response.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)
        except (urllib.error.URLError, OSError):
            return None


class Spawner:
    """Indirection over ``subprocess.Popen`` for detached servers.

    Every server this script starts must outlive the ``up`` invocation (so the
    user can explore the tab / the agent can drive the port), so all spawns are
    detached and later killed by ``down`` via the recorded pid.
    """

    def spawn_detached(
        self, argv: Sequence[str], cwd: str, env: dict, log_path: str
    ) -> int:
        """Start a long-lived process in its own session; return its pid.

        ``start_new_session=True`` makes the child a session/process-group leader
        so it survives this script exiting and so ``down`` can signal the whole
        group, reaping any grandchildren ``uv run`` spawns. Output is appended to
        ``log_path`` so a failed boot is diagnosable.
        """
        with open(log_path, "ab") as log_file:
            process = subprocess.Popen(
                list(argv),
                cwd=cwd,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        return int(process.pid)


def find_free_port() -> int:
    """Bind to an ephemeral port, then release it for the server to take."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_healthy(
    http: HttpClient,
    url: str,
    attempts: int,
    interval: float,
    sleeper: Callable[[float], None],
) -> bool:
    """Poll ``url`` until it returns HTTP 200, up to ``attempts`` times."""
    for index in range(attempts):
        if http.get_status(url, timeout=5.0) == 200:
            return True
        if index < attempts - 1:
            sleeper(interval)
    return False


def _wait_process_gone(
    runner: Runner,
    pid: int,
    attempts: int,
    interval: float,
    sleeper: Callable[[float], None],
) -> bool:
    """Poll until the process group led by ``pid`` is gone, up to ``attempts``."""
    for index in range(attempts):
        if not runner.process_group_alive(pid):
            return True
        if index < attempts - 1:
            sleeper(interval)
    return False


def _inner_env(
    port_env: str,
    port: int,
    host_env: str | None,
    env_overrides: dict[str, str] | None,
    unset_env: Sequence[str],
) -> dict[str, str]:
    """Build the child environment for the inner server: the ambient env with the
    isolating overrides applied and the chosen port injected. Shared by ``up``
    (fresh boot) and ``refresh`` (re-boot on the same port)."""
    env = dict(os.environ)
    for key in unset_env:
        env.pop(key, None)
    for key, value in (env_overrides or {}).items():
        env[key] = value
    env[port_env] = str(port)
    if host_env is not None:
        env[host_env] = "127.0.0.1"
    return env


def parse_env_assignments(assignments: Sequence[str]) -> dict[str, str]:
    """Parse ``NAME=VALUE`` strings into a dict. Raises on a missing ``=``."""
    parsed: dict[str, str] = {}
    for item in assignments:
        name, sep, value = item.partition("=")
        if not sep or not name:
            raise InstanceError(f"--env expects NAME=VALUE, got {item!r}")
        parsed[name] = value
    return parsed


def _state_dir(repo_root: Path, name: str) -> Path:
    return repo_root / STATE_ROOT / name


def _state_path(repo_root: Path, name: str) -> Path:
    return _state_dir(repo_root, name) / STATE_FILENAME


def _register_service(
    runner: Runner, repo_root: Path, service_name: str, port: int, what: str
) -> None:
    result = runner.run(
        [
            *FORWARD_PORT_CMD,
            "--name",
            service_name,
            "--url",
            f"http://localhost:{port}",
        ],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if getattr(result, "returncode", 0) != 0:
        stderr = (getattr(result, "stderr", "") or "").strip()
        raise InstanceError(f"{what} failed (exit {result.returncode}): {stderr}")


def _teardown(
    repo_root: Path,
    runner: Runner,
    *,
    pids: Sequence[int],
    services: Sequence[str],
) -> None:
    """Best-effort teardown of whatever ``up`` set up. Every step is unchecked so
    partial state still fully unwinds and re-runs are no-ops.

    Order: kill every detached server (by process group), then deregister every
    proxied service so the live UI stops routing to a dead port.
    """
    for pid in pids:
        runner.kill_process_group(pid)
    for service in services:
        runner.run(
            [*FORWARD_PORT_CMD, "--remove", "--name", service],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )


def up(
    name: str,
    command: Sequence[str],
    cwd: str,
    repo_root: Path,
    *,
    port_env: str,
    host_env: str | None = None,
    env_overrides: dict[str, str] | None = None,
    unset_env: Sequence[str] = (),
    health_path: str = "/",
    service_name: str | None = None,
    preview_service_name: str | None = None,
    preview_title: str | None = None,
    runner: Runner,
    http: HttpClient,
    spawner: Spawner,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    """Boot an isolated instance of a service; optionally register + wrap it.

    Picks a free port, injects it into ``port_env`` (and ``host_env`` when given)
    on top of ``env_overrides`` / ``unset_env``, launches ``command`` from ``cwd``
    detached, and waits for ``health_path`` to serve 200. With ``service_name`` it
    also registers the instance as a proxied service; with the preview names +
    title it additionally boots the labeled wrapper frame.

    On any failure the partial state is torn down and 1 is returned. On success a
    state file records the servers + services so ``down`` can find them later.
    """
    if not command:
        sys.stderr.write("up: no launch command given (pass it after `--`).\n")
        return 1
    if not Path(cwd).is_dir():
        sys.stderr.write(f"up: --cwd {cwd} is not a directory.\n")
        return 1
    preview_requested = preview_title is not None or preview_service_name is not None
    if preview_requested and not (
        preview_title and preview_service_name and service_name
    ):
        sys.stderr.write(
            "up: a preview needs --service-name, --preview-service-name, and "
            "--preview-title together.\n"
        )
        return 1

    # Clear any stale instance for this name first so a re-run is clean.
    down(name, repo_root, runner=runner)

    state_dir = _state_dir(repo_root, name)
    inner_log_path = state_dir / INNER_LOG_FILENAME
    wrapper_log_path = state_dir / WRAPPER_LOG_FILENAME
    state_dir.mkdir(parents=True, exist_ok=True)

    # Track what has been stood up so teardown unwinds exactly the partial state
    # on any failure (each server/service is appended right after it is created).
    pids: list[int] = []
    services: list[str] = []
    try:
        # 1. Boot the service on a free port, with the isolating env overrides.
        inner_port = find_free_port()
        env = _inner_env(port_env, inner_port, host_env, env_overrides, unset_env)
        pids.append(
            spawner.spawn_detached(
                list(command),
                cwd=cwd,
                env=env,
                log_path=str(inner_log_path),
            )
        )
        inner_url = f"http://127.0.0.1:{inner_port}"
        if not wait_healthy(
            http,
            f"{inner_url}{health_path}",
            _HEALTH_ATTEMPTS,
            _HEALTH_INTERVAL_SECONDS,
            sleeper,
        ):
            raise InstanceError(
                f"instance did not become healthy on port {inner_port} "
                f"(see {inner_log_path})"
            )

        # 2. Register it as a proxied service, if asked.
        if service_name is not None:
            _register_service(
                runner, repo_root, service_name, inner_port, "forward_port register"
            )
            services.append(service_name)

        # 3. Wrap it in a labeled preview frame, if asked.
        wrapper_port: int | None = None
        if preview_requested:
            # Validated up front: a preview implies all three names are set. Re-assert
            # so the invariant is explicit (and the types narrow from ``str | None``).
            assert (
                service_name is not None
                and preview_service_name is not None
                and preview_title is not None
            )
            wrapper_port = find_free_port()
            pids.append(
                spawner.spawn_detached(
                    [
                        sys.executable,
                        str(_WRAPPER_SCRIPT_PATH),
                        "--port",
                        str(wrapper_port),
                        "--inner-service",
                        service_name,
                        "--title",
                        preview_title,
                    ],
                    cwd=str(repo_root),
                    env=dict(os.environ),
                    log_path=str(wrapper_log_path),
                )
            )
            if not wait_healthy(
                http,
                f"http://127.0.0.1:{wrapper_port}/",
                _HEALTH_ATTEMPTS,
                _HEALTH_INTERVAL_SECONDS,
                sleeper,
            ):
                raise InstanceError(
                    f"preview wrapper did not become healthy on port {wrapper_port} "
                    f"(see {wrapper_log_path})"
                )
            _register_service(
                runner,
                repo_root,
                preview_service_name,
                wrapper_port,
                "forward_port register (wrapper)",
            )
            services.append(preview_service_name)

        state = {
            "name": name,
            "cwd": str(cwd),
            "inner_port": inner_port,
            "wrapper_port": wrapper_port,
            "pids": pids,
            "services": services,
            "inner_log": str(inner_log_path),
            "wrapper_log": str(wrapper_log_path) if preview_requested else None,
            # The recipe to re-boot the inner server (pids[0]) on the same port,
            # so ``refresh`` can replay it without re-passing any of it. The inner
            # server is always the first spawn, so pids[0] is the one refresh
            # cycles; the wrapper (pids[1], if any) is left running.
            "inner": {
                "command": list(command),
                "cwd": str(cwd),
                "port": inner_port,
                "port_env": port_env,
                "host_env": host_env,
                "env_overrides": dict(env_overrides or {}),
                "unset_env": list(unset_env),
                "health_path": health_path,
                "log": str(inner_log_path),
            },
        }
        _state_path(repo_root, name).write_text(json.dumps(state, indent=2))
    except (InstanceError, OSError) as exc:
        # OSError too: a boot can fail by raising rather than exiting non-zero -- a
        # missing ``uv`` binary surfaces as FileNotFoundError, and find_free_port
        # can raise a socket OSError. Either way a server may already be running,
        # so teardown must run.
        sys.stderr.write(f"up failed: {exc}\ntearing down partial instance...\n")
        _teardown(repo_root, runner, pids=pids, services=services)
        shutil.rmtree(state_dir, ignore_errors=True)
        return 1

    # The user-/agent-facing URL: the wrapper tab when previewing, else the
    # instance's own loopback port. Emit it on stdout so the caller can capture it.
    if preview_requested:
        sys.stdout.write(f"/service/{preview_service_name}/\n")
        sys.stderr.write(
            f"preview up: open the '{preview_service_name}' service tab (serving {cwd} "
            f"on port {inner_port}, wrapped on port {wrapper_port}). Run "
            f"'down --name {name}' to tear it down.\n"
        )
    else:
        sys.stdout.write(f"{inner_url}\n")
        sys.stderr.write(
            f"instance up: reach it at {inner_url} (serving {cwd}). Run "
            f"'down --name {name}' to tear it down.\n"
        )
    return 0


def down(name: str, repo_root: Path, *, runner: Runner) -> int:
    """Tear down the instance for ``name``: kill the server(s), deregister the
    service(s), delete the state directory.

    Idempotent: a missing state file is a no-op success, so this is safe to run to
    clean up after a successful test, a rejected preview, or a half-set-up
    instance. Returns 0 unless the state file is unreadable.
    """
    state_path = _state_path(repo_root, name)
    if not state_path.exists():
        sys.stderr.write(f"no active instance for '{name}'; nothing to tear down.\n")
        return 0
    try:
        state = json.loads(state_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        sys.stderr.write(f"error: could not read instance state {state_path}: {exc}\n")
        return 1
    pids = state.get("pids") or []
    services = state.get("services") or []
    _teardown(repo_root, runner, pids=pids, services=services)
    shutil.rmtree(_state_dir(repo_root, name), ignore_errors=True)
    sys.stderr.write(f"instance for '{name}' torn down.\n")
    return 0


def refresh(
    name: str,
    repo_root: Path,
    *,
    runner: Runner,
    http: HttpClient,
    spawner: Spawner,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    """Re-boot the inner server of instance ``name`` on its existing port.

    This is the in-place update motion: the caller has rebuilt/edited the code the
    inner server runs from, and wants the live instance to pick it up *without*
    changing its port, tearing down the wrapper, or moving the user's tab. We stop
    the inner server (``pids[0]``), wait for it to release its port, relaunch the
    exact command recorded at ``up`` time on the same port, and re-probe health.
    The wrapper (``pids[1]``, if any) and both service registrations are left
    untouched -- since the port is unchanged, they keep routing to the new
    process. The caller reloads the tab's iframe itself (this never touches it).

    Returns 0 once the rebooted inner server is healthy; 1 if there is no
    refreshable instance, the old server would not exit, or the new one did not
    come up (in which case the preview tab shows an error until the underlying
    build is fixed and refresh is retried -- but nothing else was disturbed).
    """
    state_path = _state_path(repo_root, name)
    if not state_path.exists():
        sys.stderr.write(
            f"no active instance for '{name}'; nothing to refresh (run `up` first).\n"
        )
        return 1
    try:
        state = json.loads(state_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        sys.stderr.write(f"error: could not read instance state {state_path}: {exc}\n")
        return 1
    inner = state.get("inner")
    pids = list(state.get("pids") or [])
    if not inner or not pids:
        sys.stderr.write(
            f"instance '{name}' has no recorded inner-boot recipe; cannot refresh "
            "(tear it down and re-create it with `up`).\n"
        )
        return 1

    old_pid = int(pids[0])
    port = int(inner["port"])
    # 1. Stop the old inner server and wait for it to release the port. A live
    #    listening socket cannot be rebound, so we must not respawn until it is
    #    gone -- otherwise the new process fails to bind.
    runner.kill_process_group(old_pid)
    if not _wait_process_gone(
        runner, old_pid, _STOP_ATTEMPTS, _STOP_INTERVAL_SECONDS, sleeper
    ):
        sys.stderr.write(
            f"refresh: inner server pid {old_pid} did not exit in time; its port "
            f"{port} may still be held. Aborting rather than risk a bind clash.\n"
        )
        return 1

    # 2. Relaunch the recorded inner command on the SAME port.
    env = _inner_env(
        inner["port_env"],
        port,
        inner.get("host_env"),
        inner.get("env_overrides") or {},
        inner.get("unset_env") or [],
    )
    try:
        new_pid = spawner.spawn_detached(
            list(inner["command"]),
            cwd=inner["cwd"],
            env=env,
            log_path=inner["log"],
        )
    except OSError as exc:
        sys.stderr.write(f"refresh: failed to relaunch the inner server: {exc}\n")
        return 1
    # Record the new pid *before* the health wait so ``down`` can still kill it
    # even if it never becomes healthy (no leaked server).
    pids[0] = new_pid
    state["pids"] = pids
    state_path.write_text(json.dumps(state, indent=2))

    if not wait_healthy(
        http,
        f"http://127.0.0.1:{port}{inner['health_path']}",
        _HEALTH_ATTEMPTS,
        _HEALTH_INTERVAL_SECONDS,
        sleeper,
    ):
        sys.stderr.write(
            f"refresh: inner server did not become healthy on port {port} after "
            f"reboot (see {inner['log']}). The preview tab will show an error until "
            "the underlying build boots; fix it and refresh again.\n"
        )
        return 1
    sys.stderr.write(
        f"refresh: inner server for '{name}' rebooted on port {port}; reload the "
        "tab to see the current build.\n"
    )
    return 0


def _add_repo_root_arg(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument(
        "--repo-root",
        default=".",
        help="Path to the repository root (default: current directory).",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Spin up an isolated, throwaway instance of a service on a spare port "
            "-- for the agent's own testing, or surfaced to the user as a preview."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    up_parser = subparsers.add_parser(
        "up", help="Boot an isolated instance (optionally as a previewable tab)."
    )
    up_parser.add_argument(
        "--name",
        required=True,
        help="Short slug identifying this instance (names the state dir).",
    )
    up_parser.add_argument(
        "--cwd", required=True, help="Directory to launch the service from."
    )
    up_parser.add_argument(
        "--port-env",
        required=True,
        help="Env var the service reads its port from; the chosen free port is "
        "injected into it (e.g. SYSTEM_INTERFACE_PORT, MYSVC_PORT).",
    )
    up_parser.add_argument(
        "--host-env",
        default=None,
        help="Optional env var to set to 127.0.0.1 (for services that bind a "
        "configurable host, e.g. SYSTEM_INTERFACE_HOST).",
    )
    up_parser.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Env override for the instance (repeatable); e.g. point a *_DATA_DIR "
        "at a scratch copy of the data.",
    )
    up_parser.add_argument(
        "--unset-env",
        action="append",
        default=[],
        metavar="NAME",
        help="Env var to remove for the instance (repeatable).",
    )
    up_parser.add_argument(
        "--health-path",
        default="/",
        help="Path polled for a 200 to decide the instance is up (default: /).",
    )
    up_parser.add_argument(
        "--service-name",
        default=None,
        help="Register the instance as this proxied service (needed to surface it "
        "as a tab). Omit for a bare instance reached directly on its port.",
    )
    up_parser.add_argument(
        "--preview-service-name",
        default=None,
        help="Register the labeled preview-frame wrapper as this service (the tab "
        "the user opens). Requires --service-name and --preview-title.",
    )
    up_parser.add_argument(
        "--preview-title",
        default=None,
        help="Human-readable label shown in the preview frame banner.",
    )
    _add_repo_root_arg(up_parser)
    up_parser.add_argument(
        "launch",
        nargs=argparse.REMAINDER,
        help="The launch argv, after `--` (e.g. `-- uv run my-service`).",
    )

    down_parser = subparsers.add_parser(
        "down",
        help="Tear down an instance (kill the server(s), deregister service(s)). "
        "Idempotent.",
    )
    down_parser.add_argument("--name", required=True, help="The name passed to 'up'.")
    _add_repo_root_arg(down_parser)

    refresh_parser = subparsers.add_parser(
        "refresh",
        help="Re-boot the inner server on its existing port (to pick up a rebuild "
        "/ edit) without changing the port, wrapper, or the user's tab.",
    )
    refresh_parser.add_argument(
        "--name", required=True, help="The name passed to 'up'."
    )
    _add_repo_root_arg(refresh_parser)

    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    if args.command == "up":
        # argparse.REMAINDER keeps a leading `--`; drop it so ``command`` is the
        # bare launch argv.
        launch = list(args.launch)
        if launch and launch[0] == "--":
            launch = launch[1:]
        try:
            env_overrides = parse_env_assignments(args.env)
        except InstanceError as exc:
            sys.stderr.write(f"error: {exc}\n")
            return 1
        return up(
            args.name,
            launch,
            args.cwd,
            repo_root,
            port_env=args.port_env,
            host_env=args.host_env,
            env_overrides=env_overrides,
            unset_env=args.unset_env,
            health_path=args.health_path,
            service_name=args.service_name,
            preview_service_name=args.preview_service_name,
            preview_title=args.preview_title,
            runner=Runner(),
            http=HttpClient(),
            spawner=Spawner(),
        )
    if args.command == "down":
        return down(args.name, repo_root, runner=Runner())
    if args.command == "refresh":
        return refresh(
            args.name,
            repo_root,
            runner=Runner(),
            http=HttpClient(),
            spawner=Spawner(),
        )
    parser.error(f"unknown command: {args.command}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
