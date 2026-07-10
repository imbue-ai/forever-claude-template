"""Rich TypeScript/JavaScript intelligence via a persistent language service.

Used only for repos the user has explicitly *prepared* (see ``prepare.py`` --
dependencies installed and ``typescript`` available). Spawns one Node helper
(``assets/tsintel_server.mjs``) per prepared tree, speaking a line-delimited JSON
protocol, and exposes ``hover()`` / ``definition()`` with the same signatures and
response shapes as ``jsintel`` so ``runner`` can choose either engine
transparently.

Contract: both functions return ``None`` on anything that is not a positive
result -- no server, empty result, protocol error, dead process -- so the caller
cleanly falls back to the tree-sitter engine. The server-spawning step is an
injected seam (``server_factory``) so tests never launch a real Node process.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from pr_review import prepare
from pr_review.github import RepoTree

_HELPER = Path(__file__).parent / "assets" / "tsintel_server.mjs"
_IDLE_SECONDS = 600
# Bound the startup handshake: the helper emits its ready line synchronously
# right after loading typescript, so a spawn that has not spoken within this
# window is wedged. Without the bound its blocking readline would run under the
# global registry lock (see _get_server) and freeze JS intel for every tree.
_STARTUP_TIMEOUT_S = 30


class _ServerError(RuntimeError):
    """The language-service subprocess failed or answered unusably."""


class _Server:
    """A single Node language-service process, one request at a time (locked)."""

    def __init__(self, proc: subprocess.Popen) -> None:
        self._proc = proc
        self._lock = threading.Lock()
        self._id = 0
        self.last_used = time.monotonic()

    def alive(self) -> bool:
        return self._proc.poll() is None

    def request(self, payload: dict) -> dict:
        with self._lock:
            if self._proc.poll() is not None or self._proc.stdin is None or self._proc.stdout is None:
                raise _ServerError("language service is not running")
            self._id += 1
            try:
                self._proc.stdin.write(json.dumps({**payload, "id": self._id}) + "\n")
                self._proc.stdin.flush()
                line = self._proc.stdout.readline()
            except (OSError, ValueError) as exc:
                raise _ServerError(str(exc)) from exc
            self.last_used = time.monotonic()
            if not line:
                raise _ServerError("no response from language service")
            try:
                data = json.loads(line)
            except ValueError as exc:
                raise _ServerError(f"unparseable response: {exc}") from exc
            if not isinstance(data, dict):
                raise _ServerError("non-object response")
            return data

    def close(self) -> None:
        _terminate(self._proc)


def _terminate(proc: subprocess.Popen) -> None:
    """Close the parent-side pipes and terminate ``proc`` (idempotent, best-effort).

    Used both when retiring a live server and when tearing down a helper that
    failed to start, so the pipe file descriptors are always released rather than
    left dangling until garbage collection.
    """
    for stream in (proc.stdin, proc.stdout):
        try:
            if stream is not None:
                stream.close()
        except OSError:
            pass
    try:
        proc.terminate()
    except OSError:
        pass


# Registry of one server per prepared tree. Guarded by a lock because the Flask
# server is threaded.
ServerFactory = Callable[[RepoTree], "_Server | None"]
_servers: dict[str, _Server] = {}
_registry_lock = threading.Lock()


def _spawn_server(tree: RepoTree) -> _Server | None:
    """Start the Node helper for a prepared tree, or None if it cannot start."""
    status = prepare.prepare_status(tree)
    ts_dir_rel = status.get("typescript_dir")
    if not isinstance(ts_dir_rel, str) or not ts_dir_rel:
        return None
    ts_dir = (tree.root / ts_dir_rel).resolve()
    if not ts_dir.is_dir():
        return None
    try:
        proc = subprocess.Popen(
            ["node", str(_HELPER), str(tree.root), str(ts_dir)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
    except OSError:
        return None
    # Kill a helper that never emits its ready line so the readline below cannot
    # block forever under the caller's registry lock.
    killer = threading.Timer(_STARTUP_TIMEOUT_S, proc.kill)
    killer.start()
    try:
        ready_line = proc.stdout.readline() if proc.stdout is not None else ""
    except (OSError, ValueError):
        # A kill (startup timeout) can close the pipe out from under readline.
        ready_line = ""
    finally:
        killer.cancel()
    try:
        ready = json.loads(ready_line)
    except ValueError:
        ready = {}
    if not (isinstance(ready, dict) and ready.get("ready")):
        _terminate(proc)
        return None
    return _Server(proc)


def _reap_idle_locked(exclude: str) -> None:
    now = time.monotonic()
    for key, server in list(_servers.items()):
        if key == exclude:
            continue
        if not server.alive() or now - server.last_used > _IDLE_SECONDS:
            server.close()
            _servers.pop(key, None)


def _get_server(tree: RepoTree, factory: ServerFactory) -> _Server | None:
    key = str(tree.root)
    with _registry_lock:
        _reap_idle_locked(exclude=key)
        server = _servers.get(key)
        if server is not None and not server.alive():
            server.close()
            server = None
            _servers.pop(key, None)
        if server is None:
            server = factory(tree)
            if server is None:
                return None
            _servers[key] = server
        return server


def _drop(tree: RepoTree) -> None:
    with _registry_lock:
        server = _servers.pop(str(tree.root), None)
    if server is not None:
        server.close()


def reset() -> None:
    """Close and forget all servers (used by tests)."""
    with _registry_lock:
        for server in _servers.values():
            server.close()
        _servers.clear()


def hover(tree: RepoTree, rel_path: str, line: int, column: int, server_factory: ServerFactory | None = None) -> dict | None:
    server = _get_server(tree, server_factory or _spawn_server)
    if server is None:
        return None
    try:
        resp = server.request({"op": "hover", "path": rel_path, "line": line, "col": column})
    except _ServerError:
        _drop(tree)
        return None
    if resp.get("error"):
        return None
    contents = resp.get("contents")
    return {"contents": contents} if contents else None


def definition(tree: RepoTree, rel_path: str, line: int, column: int, server_factory: ServerFactory | None = None) -> dict | None:
    server = _get_server(tree, server_factory or _spawn_server)
    if server is None:
        return None
    try:
        resp = server.request({"op": "def", "path": rel_path, "line": line, "col": column})
    except _ServerError:
        _drop(tree)
        return None
    if resp.get("error") or not resp.get("found"):
        return None
    return {
        "in_repo": bool(resp.get("in_repo")),
        "path": resp.get("path"),
        "line": resp.get("line", 1),
        "column": resp.get("column", 1),
        "name": resp.get("name"),
        "type": resp.get("type"),
    }
