#!/usr/bin/env python3
"""Reveal a merged system-interface change to the live UI -- and auto-recover if it breaks.

This is the reveal step of the ``update-system-interface`` flow. The lead agent
merges a verified worker branch into the served working tree, then runs this
script. It owns the *entire* reveal sequence as a single deterministic motion,
because the failure mode is catastrophic: if the ``system-interface`` backend
fails to start, the user loses their whole chat UI and there is nowhere left to
surface an error message. So detection is not enough -- this script must always
leave the served UI in a working state, on its own, without the agent.

What it does, given the pre-merge revision (``--rollback-to``):

1. Refuse to run on a dirty tree (so a rollback can never clobber unrelated work).
2. Classify what changed since the known-good revision (frontend src / frontend
   manifest / backend src / backend manifest).
3. Refresh dependencies only if a manifest changed (``npm ci`` / ``uv tool
   install -e apps/system_interface --reinstall``). A plain restart does NOT
   re-resolve the editable tool's dependencies, so a backend dependency add
   would otherwise crash the service on restart.
4. For a backend change, *pre-flight* the merged code on a throwaway port before
   touching the live service -- if it cannot boot, the live service is never
   restarted and we go straight to rollback (the UI never went down).
5. Build the frontend bundle, restart the backend, and tell open browsers to
   reload, as applicable.
6. Probe the live service's loopback endpoint until healthy (with a deadline).
7. On ANY failure, restore the served tree to the known-good revision (as a
   forward revert commit) and re-probe to *confirm* the UI is back. The live
   backend is restarted during recovery only if the failed reveal had already
   restarted it (a failed post-restart health check); when the failure happened
   before the live restart (pre-flight, dependency refresh, frontend build) the
   live service is still serving known-good code and is left untouched, so the
   UI never blips. Only then does the script exit -- reporting what happened via
   its exit code and stderr.

Run via bare ``python3`` (standard library only), like ``forward_port.py`` and
``reload_system_interface``'s predecessor -- it orchestrates the environment, so
it must not depend on any particular venv being synced.

The same script also owns the deterministic halves of the *pre-merge preview*
gate, where the user clicks around the change before it is merged. The worker is
a local git-worktree sub-agent in this same container, so its work_dir is just a
folder it has already built -- the preview serves it in place:

- ``preview`` boots the worker's ``--work-dir`` on a free port (layout
  persistence neutered so it can't clobber the live ``layout.json``) and
  registers it as the ``si-preview-app`` service, then boots a small wrapper page
  (``preview_wrapper_server.py``) that embeds it in a labeled "preview" frame and
  registers that as the user-facing ``si-preview`` service. The live UI proxies
  ``si-preview`` as a tab that reads as a clearly-marked proposed change rather
  than a nested clone of the live UI. No fetch, no second checkout, no rebuild;
  it never touches the served tree or the worker's folder. The worker must still
  exist at preview time.
- ``unpreview`` tears that down (kill both servers, deregister both services).
  Idempotent.

The non-deterministic part -- opening the tab and gating on the user's judgment
-- stays with the agent.

Usage:
    python3 reveal_system_interface.py reveal --rollback-to <pre-merge-sha> [--repo-root PATH]
    python3 reveal_system_interface.py preview --slug <name> --work-dir <worker-work-dir> [--repo-root PATH]
    python3 reveal_system_interface.py unpreview --slug <name> [--repo-root PATH]

Environment:
    MINDS_WORKSPACE_SERVER_URL  Base URL of the live workspace server
                                (default http://127.0.0.1:8000).
    MNGR_AGENT_ID               Sent for telemetry on the reload broadcast.

Exit codes (``reveal``):
    0  Revealed successfully; live UI is healthy.
    1  Precondition error (dirty tree, bad arguments) -- nothing was changed.
    2  The change was bad and was rolled back; the live UI is confirmed healthy
       on the known-good revision (the requested change did NOT land).
    3  EMERGENCY: even rollback could not restore a healthy UI.

Exit codes (``preview`` / ``unpreview``):
    0  Success (preview is up / torn down).
    1  The preview failed to build or boot (and tore itself down), or a bad
       argument / unreadable state file.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

DEFAULT_WORKSPACE_URL = "http://127.0.0.1:8000"
ENV_WORKSPACE_URL = "MINDS_WORKSPACE_SERVER_URL"
ENV_MNGR_AGENT_ID = "MNGR_AGENT_ID"
MNGR_AGENT_ID_HEADER = "X-Mngr-Agent-Id"

# The served app, the editable tool the live service runs from, and the build
# surfaces. These mirror scripts/build_workspace.sh -- the source of truth for
# how the served environment is constructed.
APP_DIR = "apps/system_interface"
FRONTEND_DIR = f"{APP_DIR}/frontend"
TOOL_NAME = "system-interface"
RELOAD_OP = "reload_system_interface"

# Pre-merge preview: boot the worker's own (already-built) work_dir as a service
# the live UI proxies, so the user can click around the change before it is
# merged. The worker is a local git-worktree sub-agent in this same container,
# so its work_dir is just a built folder on disk -- no fetch or rebuild needed.
# See ``preview`` / ``unpreview``.
#
# The preview is registered under fixed service names -- the
# update-system-interface flow runs one preview at a time, and ``preview``
# tears down any stale preview for the slug first. State (the detached pids, the
# ports, the service names, the worker work_dir) lives under the lead's
# ``runtime/`` so it is gitignored and survives between the separate ``preview``
# and ``unpreview`` invocations.
#
# Two services are registered, not one: the inner service points at the worker's
# booted app, and the outer "wrapper" service the user actually opens points at a
# tiny chrome page (``preview_wrapper_server.py``) that embeds the inner service
# in a labeled "preview" frame. Wrapping it this way keeps the preview from
# reading as a confusing nested clone of the live UI. The two live at disjoint
# ``/service/<name>/`` scopes so the dispatcher's scoped service workers do not
# interfere.
PREVIEW_SERVICE_NAME = "si-preview"
PREVIEW_INNER_SERVICE_NAME = "si-preview-app"
PREVIEW_WRAPPER_SCRIPT = "preview_wrapper_server.py"
PREVIEW_STATE_ROOT = "runtime/system-interface-preview"
PREVIEW_STATE_FILENAME = "preview.json"
PREVIEW_LOG_FILENAME = "preview.log"
PREVIEW_WRAPPER_LOG_FILENAME = "preview-wrapper.log"
# The wrapper server ships beside this script and is stdlib-only, so it runs
# under the same bare ``python3`` that runs this script -- no venv resolution.
_WRAPPER_SCRIPT_PATH = Path(__file__).resolve().parent / PREVIEW_WRAPPER_SCRIPT
# forward_port.py imports tomlkit (a venv dependency), but this script is run via
# bare python3 with no venv assumed, and the ambient ``python3`` need not have
# tomlkit. Invoke it through ``uv run`` (like scripts/run_ttyd.sh) so the
# dependency is always resolved regardless of the caller's environment.
FORWARD_PORT_CMD = ("uv", "run", "python3", "scripts/forward_port.py")

# Endpoints used to probe liveness. ``/api/agents`` exercises the mngr plugin
# discovery path -- exactly what a missing backend dependency or a broken
# plugin-config parse would take down -- so a 200 there is a strong "the backend
# actually works" signal, not just "the server is listening".
HEALTH_PATH = "/api/agents"
SERVE_PATH = "/"

# Poll budget for "did the service come back up". Restart is fire-and-forget, so
# we poll rather than assume.
_HEALTH_ATTEMPTS = 30
_HEALTH_INTERVAL_SECONDS = 1.0
# Pre-flight boot is a fresh process on a throwaway port; give it the same grace.
_PREFLIGHT_ATTEMPTS = 30
_PREFLIGHT_INTERVAL_SECONDS = 1.0
# A preview boots a full instance from the worker's work_dir; give it a longer
# grace than the pre-flight since first import + startup runs alongside the live
# service on the same box.
_PREVIEW_ATTEMPTS = 60
_PREVIEW_INTERVAL_SECONDS = 1.0


class RevealError(Exception):
    """Base class for reveal failures (avoids raising built-in exceptions)."""


class PreconditionError(RevealError):
    """A precondition was not met; nothing was changed, do not roll back."""


class RevealFailed(RevealError):
    """The reveal of the merged change failed; the caller must roll back.

    ``live_service_restarted`` records whether the live service was already
    (re)started before the failure. It is ``False`` for failures that happen
    before the live restart (pre-flight, dependency refresh, frontend build) --
    in which case the live service is untouched and still serving known-good
    code, so recovery must NOT restart it -- and ``True`` once the restart has
    been attempted, where recovery must restart to reload known-good code.
    """

    def __init__(self, message: str, *, live_service_restarted: bool = False) -> None:
        super().__init__(message)
        self.live_service_restarted = live_service_restarted


@dataclass(frozen=True)
class ChangeSet:
    """Which kinds of system-interface change a diff contains."""

    frontend_src: bool
    frontend_manifest: bool
    backend_src: bool
    backend_manifest: bool

    @property
    def frontend(self) -> bool:
        return self.frontend_src or self.frontend_manifest

    @property
    def backend(self) -> bool:
        return self.backend_src or self.backend_manifest

    @property
    def any(self) -> bool:
        return self.frontend or self.backend


class Runner:
    """Indirection over ``subprocess.run`` so tests can intercept commands.

    The default implementation calls ``subprocess.run`` directly; tests inject a
    recording stub instead.
    """

    def run(self, argv: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(list(argv), **kwargs)


class HttpClient:
    """Indirection over the loopback HTTP calls (health probe + reload broadcast)."""

    def get_status(self, url: str, timeout: float) -> int | None:
        """Return the HTTP status for a GET, or ``None`` if the host is unreachable."""
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return int(response.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)
        except (urllib.error.URLError, OSError):
            return None

    def post_json(
        self, url: str, payload: dict, headers: dict, timeout: float
    ) -> int | None:
        """POST a JSON body; return the HTTP status or ``None`` if unreachable."""
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return int(response.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)
        except (urllib.error.URLError, OSError):
            return None


@dataclass
class Spawned:
    """A handle to a spawned throwaway server process."""

    _process: subprocess.Popen

    def terminate(self) -> None:
        self._process.terminate()
        try:
            self._process.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            self._process.kill()


class Spawner:
    """Indirection over ``subprocess.Popen`` for spawned servers.

    ``spawn`` returns a managed child (terminated in a ``finally``) for the
    pre-flight throwaway boot. ``spawn_detached`` starts a process in its own
    session and returns only its pid -- used for the preview server, which must
    outlive this ``preview`` invocation so the user can explore the tab; it is
    later killed by ``unpreview`` via the recorded pid.
    """

    def spawn(self, argv: Sequence[str], cwd: str, env: dict) -> Spawned:
        process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return Spawned(_process=process)

    def spawn_detached(
        self, argv: Sequence[str], cwd: str, env: dict, log_path: str | None = None
    ) -> int:
        """Start a long-lived process in its own session; return its pid.

        ``start_new_session=True`` makes the child a session/process-group
        leader so it survives this script exiting and so ``unpreview`` can
        signal the whole group (``kill -- -<pid>``), reaping any grandchildren
        ``uv run`` spawns. Output goes to ``log_path`` (appended) when given so
        a failed boot is diagnosable, else to ``/dev/null``.
        """
        if log_path is not None:
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
        process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return int(process.pid)


def classify_changes(paths: Sequence[str]) -> ChangeSet:
    """Classify repo-relative changed ``paths`` into a :class:`ChangeSet`.

    The frontend build output (``.../static/``) is gitignored and so never
    appears in a diff; we do not need to special-case it here.
    """
    frontend_src = False
    frontend_manifest = False
    backend_src = False
    backend_manifest = False
    for path in paths:
        if path in (
            f"{FRONTEND_DIR}/package.json",
            f"{FRONTEND_DIR}/package-lock.json",
        ):
            frontend_manifest = True
        elif path.startswith(f"{FRONTEND_DIR}/src/"):
            frontend_src = True
        elif path == f"{APP_DIR}/pyproject.toml" or path == "uv.lock":
            backend_manifest = True
        elif (
            path.startswith(f"{APP_DIR}/imbue/")
            and path.endswith(".py")
            and not _is_test_file(path)
        ):
            backend_src = True
    return ChangeSet(
        frontend_src=frontend_src,
        frontend_manifest=frontend_manifest,
        backend_src=backend_src,
        backend_manifest=backend_manifest,
    )


def _is_test_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name.endswith("_test.py") or name.startswith("test_")


def find_free_port() -> int:
    """Bind to an ephemeral port, then release it for the throwaway server to take."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _diff_name_status(
    repo_root: Path, rollback_to: str, runner: Runner
) -> list[tuple[str, str]]:
    """Return ``(status, path)`` pairs for ``rollback_to..HEAD``.

    ``--no-renames`` makes a rename surface as a delete + add pair, which keeps
    the rollback logic simple (restore the deletes, remove the adds).
    """
    result = runner.run(
        ["git", "diff", "--no-renames", "--name-status", rollback_to, "HEAD"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )
    pairs: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        pairs.append((fields[0].strip(), fields[-1].strip()))
    return pairs


def _assert_clean_tree(repo_root: Path, runner: Runner) -> None:
    result = runner.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )
    if result.stdout.strip():
        raise PreconditionError(
            "working tree has uncommitted changes; refusing to reveal so a rollback "
            "can never clobber unrelated work. Commit or stash, then re-run."
        )


def _run_checked(
    runner: Runner,
    argv: Sequence[str],
    cwd: Path,
    what: str,
    *,
    live_service_restarted: bool = False,
) -> None:
    """Run a reveal command; raise :class:`RevealFailed` on a non-zero exit.

    ``live_service_restarted`` is forwarded onto the raised exception so callers
    that run the live restart can record that recovery must restart (see
    :class:`RevealFailed`)."""
    result = runner.run(
        list(argv), cwd=str(cwd), capture_output=True, text=True, check=False
    )
    if getattr(result, "returncode", 0) != 0:
        stderr = (getattr(result, "stderr", "") or "").strip()
        raise RevealFailed(
            f"{what} failed (exit {result.returncode}): {stderr}",
            live_service_restarted=live_service_restarted,
        )


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


def _preflight_ok(
    repo_root: Path,
    http: HttpClient,
    spawner: Spawner,
    sleeper: Callable[[float], None],
) -> bool:
    """Boot the merged backend on a throwaway port and probe it, without touching
    the live service. Returns True iff it serves a healthy response."""
    port = find_free_port()
    env = dict(os.environ)
    env["SYSTEM_INTERFACE_HOST"] = "127.0.0.1"
    env["SYSTEM_INTERFACE_PORT"] = str(port)
    spawned = spawner.spawn([TOOL_NAME], cwd=str(repo_root / APP_DIR), env=env)
    try:
        return wait_healthy(
            http,
            f"http://127.0.0.1:{port}{HEALTH_PATH}",
            _PREFLIGHT_ATTEMPTS,
            _PREFLIGHT_INTERVAL_SECONDS,
            sleeper,
        )
    finally:
        spawned.terminate()


def _broadcast_reload(http: HttpClient, base_url: str) -> None:
    """Tell open browsers to reload the whole UI. Best-effort: a no-op when no
    browser is connected, and never fatal on its own."""
    agent_id = os.environ.get(ENV_MNGR_AGENT_ID, "")
    status = http.post_json(
        f"{base_url}/api/layout/broadcast",
        {"op": RELOAD_OP, "args": {}, "agent_id": agent_id},
        {"Content-Type": "application/json", MNGR_AGENT_ID_HEADER: agent_id},
        timeout=10.0,
    )
    if status != 200:
        sys.stderr.write(
            f"warning: reload broadcast returned {status}; if a browser is open it may "
            "not have refreshed (the new bundle is still on disk and will load on next visit).\n"
        )


def _refresh_dependencies(changes: ChangeSet, repo_root: Path, runner: Runner) -> None:
    if changes.frontend_manifest:
        _run_checked(runner, ["npm", "ci"], repo_root / FRONTEND_DIR, "npm ci")
    if changes.backend_manifest:
        _run_checked(
            runner,
            ["uv", "tool", "install", "-e", APP_DIR, "--reinstall"],
            repo_root,
            "uv tool install --reinstall",
        )


def _apply_reveal(
    changes: ChangeSet,
    repo_root: Path,
    base_url: str,
    runner: Runner,
    http: HttpClient,
    spawner: Spawner,
    sleeper: Callable[[float], None],
) -> None:
    """Refresh deps, build, restart, and reload as applicable. Raises
    :class:`RevealFailed` the moment any step does not end healthy."""
    _refresh_dependencies(changes, repo_root, runner)
    if changes.frontend:
        _run_checked(
            runner, ["npm", "run", "build"], repo_root / FRONTEND_DIR, "npm run build"
        )
    if changes.backend:
        if not _preflight_ok(repo_root, http, spawner, sleeper):
            # Live service was never restarted, so it is still serving known-good
            # code -- recovery must not restart it (live_service_restarted=False).
            raise RevealFailed(
                "merged backend failed to boot in a pre-flight check; live service not restarted"
            )
        # From here on the live service has been (or is being) restarted, so any
        # failure leaves it potentially running broken code: recovery must restart.
        _run_checked(
            runner,
            ["mngr", "start", "--restart", "system-services"],
            repo_root,
            "mngr start --restart",
            live_service_restarted=True,
        )
        if not wait_healthy(
            http,
            f"{base_url}{HEALTH_PATH}",
            _HEALTH_ATTEMPTS,
            _HEALTH_INTERVAL_SECONDS,
            sleeper,
        ):
            raise RevealFailed(
                "backend did not become healthy after restart",
                live_service_restarted=True,
            )
    if changes.frontend:
        _broadcast_reload(http, base_url)


def _restore_tree(
    name_status: Sequence[tuple[str, str]],
    rollback_to: str,
    repo_root: Path,
    runner: Runner,
) -> None:
    """Restore every changed path to its ``rollback_to`` state, staged for commit.

    Added-since paths are removed; modified/deleted paths are checked out from
    the known-good revision. Build output is gitignored and untouched here -- the
    recovery rebuild regenerates it.
    """
    for status, path in name_status:
        if status.startswith("A"):
            runner.run(
                ["git", "rm", "--force", "--ignore-unmatch", path],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                check=True,
            )
        else:
            runner.run(
                ["git", "checkout", rollback_to, "--", path],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                check=True,
            )


def _commit_rollback(
    repo_root: Path, runner: Runner, rollback_to: str, reason: str
) -> None:
    message = (
        f"Roll back system-interface reveal (restore to {rollback_to[:12]})\n\n{reason}"
    )
    runner.run(
        ["git", "commit", "--no-verify", "-m", message],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )


def _recover_running_state(
    changes: ChangeSet,
    repo_root: Path,
    base_url: str,
    runner: Runner,
    http: HttpClient,
    sleeper: Callable[[float], None],
    live_service_restarted: bool,
) -> bool:
    """After the tree is restored to known-good, rebuild/restart from it as
    needed and confirm the live UI is healthy. Returns True iff confirmed healthy.

    ``live_service_restarted`` says whether the failed reveal had already
    restarted the live backend. When it did not (pre-flight / dependency-refresh
    / frontend-build failures), the live service is still running known-good code
    in memory and the on-disk tree has just been restored to match it, so we must
    NOT restart -- doing so would needlessly blip a healthy UI. We only restart
    when the failed reveal had actually restarted the service into broken code.

    Unlike :func:`_apply_reveal`, nothing here raises -- this is the last line of
    defense, so a failed step just means "not recovered" (exit 3)."""
    try:
        _refresh_dependencies(changes, repo_root, runner)
        if changes.frontend:
            _run_checked(
                runner,
                ["npm", "run", "build"],
                repo_root / FRONTEND_DIR,
                "npm run build",
            )
        if changes.backend:
            if live_service_restarted:
                _run_checked(
                    runner,
                    ["mngr", "start", "--restart", "system-services"],
                    repo_root,
                    "mngr start --restart",
                )
            # Probe the backend health endpoint either way: after a restart to
            # confirm known-good booted, or (no restart) to confirm the untouched
            # service is still serving.
            healthy = wait_healthy(
                http,
                f"{base_url}{HEALTH_PATH}",
                _HEALTH_ATTEMPTS,
                _HEALTH_INTERVAL_SECONDS,
                sleeper,
            )
        else:
            # Frontend-only: the server was never restarted; confirm it still serves.
            healthy = wait_healthy(
                http,
                f"{base_url}{SERVE_PATH}",
                _HEALTH_ATTEMPTS,
                _HEALTH_INTERVAL_SECONDS,
                sleeper,
            )
    except RevealFailed as exc:
        sys.stderr.write(f"recovery step failed: {exc}\n")
        return False
    if healthy and changes.frontend:
        _broadcast_reload(http, base_url)
    return healthy


def reveal(
    rollback_to: str,
    repo_root: Path,
    *,
    runner: Runner,
    http: HttpClient,
    spawner: Spawner,
    sleeper: Callable[[float], None] = time.sleep,
    base_url: str | None = None,
) -> int:
    """Run the full reveal-and-recover sequence. Returns the process exit code."""
    resolved_base = (
        base_url or os.environ.get(ENV_WORKSPACE_URL, DEFAULT_WORKSPACE_URL)
    ).rstrip("/")
    _assert_clean_tree(repo_root, runner)
    name_status = _diff_name_status(repo_root, rollback_to, runner)
    changes = classify_changes([path for _, path in name_status])
    if not changes.any:
        sys.stderr.write(
            f"no system-interface changes since {rollback_to[:12]}; nothing to reveal.\n"
        )
        return 0

    try:
        _apply_reveal(changes, repo_root, resolved_base, runner, http, spawner, sleeper)
    except RevealFailed as exc:
        sys.stderr.write(
            f"reveal failed: {exc}\nrolling back to {rollback_to[:12]} and restoring the live UI...\n"
        )
        _restore_tree(name_status, rollback_to, repo_root, runner)
        _commit_rollback(
            repo_root,
            runner,
            rollback_to,
            f"Reveal failed and was auto-reverted: {exc}",
        )
        if _recover_running_state(
            changes,
            repo_root,
            resolved_base,
            runner,
            http,
            sleeper,
            live_service_restarted=exc.live_service_restarted,
        ):
            sys.stderr.write(
                "rolled back to last-known-good; the live UI is confirmed healthy. "
                "The requested change did NOT land -- diagnose it before retrying.\n"
            )
            return 2
        sys.stderr.write(
            "EMERGENCY: rollback did not restore a healthy UI. The system interface may be down; "
            "manual intervention is required.\n"
        )
        return 3

    sys.stderr.write(
        "revealed: the live system interface is updated and confirmed healthy.\n"
    )
    return 0


def _preview_state_dir(repo_root: Path, slug: str) -> Path:
    return repo_root / PREVIEW_STATE_ROOT / slug


def _preview_state_path(repo_root: Path, slug: str) -> Path:
    return _preview_state_dir(repo_root, slug) / PREVIEW_STATE_FILENAME


def _teardown_preview(
    repo_root: Path,
    runner: Runner,
    *,
    pids: Sequence[int],
    services: Sequence[str],
) -> None:
    """Best-effort teardown of whatever a preview set up. Every step is
    unchecked so partial state still fully unwinds and re-runs are no-ops.

    A preview stands up two detached servers (the worker's inner app and the
    wrapper chrome page) and registers two proxied services, so both lists may
    hold more than one entry. Order: kill every detached server (by process
    group), then deregister every proxied service so the live UI stops routing to
    a dead port. There is no worktree to remove -- the preview serves the
    worker's own work_dir in place.
    """
    for pid in pids:
        # Negative pid signals the whole process group (see ``spawn_detached``).
        runner.run(
            ["kill", "-TERM", f"-{pid}"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
    for service in services:
        runner.run(
            [*FORWARD_PORT_CMD, "--remove", "--name", service],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )


def unpreview(slug: str, repo_root: Path, *, runner: Runner) -> int:
    """Tear down the preview for ``slug``: kill the server, deregister the
    service, and delete the state directory.

    Idempotent: a missing state file is a no-op success, so this is safe to run
    on reject, after a successful reveal, or to recover from a half-set-up
    preview. Returns 0 unless the state file is unreadable.
    """
    state_path = _preview_state_path(repo_root, slug)
    if not state_path.exists():
        sys.stderr.write(f"no active preview for '{slug}'; nothing to tear down.\n")
        return 0
    try:
        state = json.loads(state_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        sys.stderr.write(f"error: could not read preview state {state_path}: {exc}\n")
        return 1
    # Newer state records lists (inner app + wrapper); fall back to the legacy
    # single-server keys so a preview created before the wrapper still tears down.
    pids = state.get("pids")
    if pids is None:
        pids = [state["pid"]] if state.get("pid") is not None else []
    services = state.get("services")
    if services is None:
        services = [state["service"]] if state.get("service") is not None else []
    _teardown_preview(repo_root, runner, pids=pids, services=services)
    shutil.rmtree(_preview_state_dir(repo_root, slug), ignore_errors=True)
    sys.stderr.write(f"preview for '{slug}' torn down.\n")
    return 0


def preview(
    slug: str,
    work_dir: str,
    repo_root: Path,
    *,
    runner: Runner,
    http: HttpClient,
    spawner: Spawner,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    """Stand up a pre-merge preview of the worker's ``work_dir`` as two proxied services.

    The worker is a local git-worktree sub-agent in this same container, so
    ``work_dir`` is a folder on disk that the worker has already built (its
    ``done`` contract runs ``uv sync`` + ``npm run build``). We boot that built
    instance detached on a free port -- with layout persistence neutered -- and
    register it as the inner service. We then boot the small wrapper chrome page
    (``preview_wrapper_server.py``) on a second port and register it as the
    user-facing service, so the tab the user opens reads as a labeled "preview"
    frame around the change rather than a confusing nested clone of the live UI.
    No fetch, no second checkout, no rebuild; the worker's folder is never
    modified (the logs and state live under the lead's ``runtime/``).

    On any failure the partial state is torn down and 1 is returned; on success
    the state file lets ``unpreview`` find both servers + services later.
    ``work_dir`` must still exist -- run this before the worker is destroyed.
    """
    # Sanity-check the work_dir before disturbing anything: a wrong --work-dir
    # should fail fast, not tear down an existing good preview for this slug.
    worker_app_dir = Path(work_dir) / APP_DIR
    if not worker_app_dir.is_dir():
        sys.stderr.write(
            f"preview: {worker_app_dir} is not a directory; is --work-dir correct "
            "and is the worker still alive (not destroyed)?\n"
        )
        return 1

    # Clear any stale preview for this slug first so the fixed service names and
    # state dir can be reused cleanly.
    unpreview(slug, repo_root, runner=runner)

    state_dir = _preview_state_dir(repo_root, slug)
    inner_log_path = state_dir / PREVIEW_LOG_FILENAME
    wrapper_log_path = state_dir / PREVIEW_WRAPPER_LOG_FILENAME
    state_dir.mkdir(parents=True, exist_ok=True)

    # Track what has been stood up so teardown unwinds exactly the partial state
    # on any failure (each server is appended right after it is spawned/registered).
    pids: list[int] = []
    services: list[str] = []
    try:
        # 1. Boot the worker's already-built app (the inner service).
        inner_port = find_free_port()
        env = dict(os.environ)
        env["SYSTEM_INTERFACE_HOST"] = "127.0.0.1"
        env["SYSTEM_INTERFACE_PORT"] = str(inner_port)
        # Drop MNGR_AGENT_ID so the preview cannot persist layout over the live
        # workspace's layout.json (the server treats a missing id as "no layout
        # dir"). MNGR_HOST_DIR is kept, so the preview still discovers and
        # renders the real agents/conversations -- a faithful look at the change.
        env.pop("MNGR_AGENT_ID", None)
        # Boot from the worker's own work_dir; ``uv run`` resolves that worktree's
        # already-synced ``.venv`` and serves its already-built ``static/`` bundle.
        pids.append(
            spawner.spawn_detached(
                ["uv", "run", TOOL_NAME],
                cwd=str(worker_app_dir),
                env=env,
                log_path=str(inner_log_path),
            )
        )
        if not wait_healthy(
            http,
            f"http://127.0.0.1:{inner_port}{HEALTH_PATH}",
            _PREVIEW_ATTEMPTS,
            _PREVIEW_INTERVAL_SECONDS,
            sleeper,
        ):
            raise RevealFailed(
                f"preview instance did not become healthy on port {inner_port} "
                f"(see {inner_log_path})"
            )
        _run_checked(
            runner,
            [
                *FORWARD_PORT_CMD,
                "--name",
                PREVIEW_INNER_SERVICE_NAME,
                "--url",
                f"http://localhost:{inner_port}",
            ],
            repo_root,
            "forward_port register (inner)",
        )
        services.append(PREVIEW_INNER_SERVICE_NAME)

        # 2. Boot the wrapper chrome page (the outer, user-facing service). It
        # embeds the inner service by its ``/service/<name>/`` path -- it needs
        # only the name, not the inner port. It is stdlib-only, so it runs under
        # the same interpreter as this script (no venv resolution).
        wrapper_port = find_free_port()
        pids.append(
            spawner.spawn_detached(
                [
                    sys.executable,
                    str(_WRAPPER_SCRIPT_PATH),
                    "--port",
                    str(wrapper_port),
                    "--inner-service",
                    PREVIEW_INNER_SERVICE_NAME,
                    "--title",
                    slug,
                ],
                cwd=str(repo_root),
                env=dict(os.environ),
                log_path=str(wrapper_log_path),
            )
        )
        if not wait_healthy(
            http,
            f"http://127.0.0.1:{wrapper_port}{SERVE_PATH}",
            _PREVIEW_ATTEMPTS,
            _PREVIEW_INTERVAL_SECONDS,
            sleeper,
        ):
            raise RevealFailed(
                f"preview wrapper did not become healthy on port {wrapper_port} "
                f"(see {wrapper_log_path})"
            )
        _run_checked(
            runner,
            [
                *FORWARD_PORT_CMD,
                "--name",
                PREVIEW_SERVICE_NAME,
                "--url",
                f"http://localhost:{wrapper_port}",
            ],
            repo_root,
            "forward_port register (wrapper)",
        )
        services.append(PREVIEW_SERVICE_NAME)

        state = {
            "slug": slug,
            "work_dir": str(work_dir),
            "inner_port": inner_port,
            "wrapper_port": wrapper_port,
            "pids": pids,
            "services": services,
            # The user-facing tab to open (the wrapper).
            "service": PREVIEW_SERVICE_NAME,
            "inner_log": str(inner_log_path),
            "wrapper_log": str(wrapper_log_path),
        }
        _preview_state_path(repo_root, slug).write_text(json.dumps(state, indent=2))
    except (RevealError, OSError) as exc:
        # OSError as well as RevealError: the boot can fail by raising rather than
        # exiting non-zero -- a missing ``uv`` binary surfaces as FileNotFoundError,
        # and ``find_free_port`` can raise a socket OSError. Either way a server may
        # already be running, so teardown must run to honor "on any failure the
        # partial state is torn down".
        sys.stderr.write(f"preview failed: {exc}\ntearing down partial preview...\n")
        _teardown_preview(repo_root, runner, pids=pids, services=services)
        shutil.rmtree(state_dir, ignore_errors=True)
        return 1

    sys.stderr.write(
        f"preview up: open the '{PREVIEW_SERVICE_NAME}' service tab to explore the "
        f"change (serving {work_dir} on port {inner_port}, wrapped on port "
        f"{wrapper_port}). Run 'unpreview --slug {slug}' to tear it down.\n"
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
            "Manage the system-interface update lifecycle: preview a worker "
            "branch before merging, reveal a merged change with auto-recovery, "
            "and tear the preview down."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    reveal_parser = subparsers.add_parser(
        "reveal", help="Reveal a merged change to the live UI, with auto-recovery."
    )
    reveal_parser.add_argument(
        "--rollback-to",
        required=True,
        help="The known-good revision to restore to if the reveal fails (the pre-merge HEAD).",
    )
    _add_repo_root_arg(reveal_parser)

    preview_parser = subparsers.add_parser(
        "preview",
        help="Boot the worker's already-built work_dir and serve it as a "
        "previewable tab, before any merge.",
    )
    preview_parser.add_argument(
        "--slug",
        required=True,
        help="Short kebab-case id for this preview (names the service/state dir).",
    )
    preview_parser.add_argument(
        "--work-dir",
        required=True,
        help="The worker's work_dir (from `mngr ls --include 'name==\"<worker>\"' "
        "--format json` -> agent.work_dir). The worker must still exist.",
    )
    _add_repo_root_arg(preview_parser)

    unpreview_parser = subparsers.add_parser(
        "unpreview",
        help="Tear down a preview (kill the server, deregister the service). Idempotent.",
    )
    unpreview_parser.add_argument(
        "--slug", required=True, help="The slug passed to 'preview'."
    )
    _add_repo_root_arg(unpreview_parser)

    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    try:
        if args.command == "reveal":
            return reveal(
                args.rollback_to,
                repo_root,
                runner=Runner(),
                http=HttpClient(),
                spawner=Spawner(),
            )
        if args.command == "preview":
            return preview(
                args.slug,
                args.work_dir,
                repo_root,
                runner=Runner(),
                http=HttpClient(),
                spawner=Spawner(),
            )
        if args.command == "unpreview":
            return unpreview(args.slug, repo_root, runner=Runner())
        parser.error(f"unknown command: {args.command}")
        return 1
    except PreconditionError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    except subprocess.CalledProcessError as exc:
        sys.stderr.write(f"error: git command failed: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
