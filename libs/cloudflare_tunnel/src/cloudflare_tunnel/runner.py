"""Cloudflare tunnel runner service.

Watches runtime/secrets for CLOUDFLARE_TUNNEL_TOKEN. When a token appears
or changes, starts (or restarts) ``cloudflared tunnel run --token <token>``.

Uses both inotify (when available) and mtime polling (10-second fallback)
to detect changes robustly. All cloudflared output is forwarded immediately
to stderr for debugging.
"""

import re
import signal
import subprocess
import sys
import time
from pathlib import Path

SECRETS_FILE = Path("runtime/secrets")
POLL_INTERVAL_SECONDS = 10
TOKEN_PATTERN = re.compile(
    r"""^export\s+CLOUDFLARE_TUNNEL_TOKEN=["']?([^"'\s]+)["']?\s*$""", re.MULTILINE
)


def _read_token(path: Path) -> str | None:
    """Extract CLOUDFLARE_TUNNEL_TOKEN from the secrets file."""
    if not path.exists():
        return None
    text = path.read_text()
    match = TOKEN_PATTERN.search(text)
    if match:
        return match.group(1)
    return None


def _try_setup_inotify(path: Path) -> object | None:
    """Try to set up inotify watching on the secrets file's parent directory.

    Returns an inotifyx file descriptor (int) or None if inotify is not available.
    """
    try:
        import inotifyx  # type: ignore[import-untyped]

        fd = inotifyx.init()
        parent = path.parent
        parent.mkdir(parents=True, exist_ok=True)
        inotifyx.add_watch(
            fd,
            str(parent),
            inotifyx.IN_MODIFY | inotifyx.IN_CREATE | inotifyx.IN_MOVED_TO,
        )
        return fd
    except (ImportError, OSError):
        return None


def _wait_for_change_inotify(fd: object, timeout_seconds: float) -> bool:
    """Wait for an inotify event, with timeout. Returns True if event received."""
    try:
        import inotifyx  # type: ignore[import-untyped]

        events = inotifyx.get_events(fd, timeout_seconds)
        return len(events) > 0
    except (ImportError, OSError):
        return False


def _run_cloudflared(token: str) -> subprocess.Popen[bytes]:
    """Start cloudflared tunnel run with the given token.

    All output goes to stderr immediately (line-buffered via stdbuf).
    """
    print(
        f"[cloudflare-tunnel] Starting cloudflared with token {token[:8]}...",
        file=sys.stderr,
        flush=True,
    )
    return subprocess.Popen(
        ["cloudflared", "tunnel", "run", "--token", token],
        stdout=sys.stderr.fileno(),
        stderr=sys.stderr.fileno(),
    )


def _stop_cloudflared(process: subprocess.Popen[bytes] | None) -> None:
    """Stop a running cloudflared process gracefully."""
    if process is None:
        return
    if process.poll() is not None:
        return
    print("[cloudflare-tunnel] Stopping cloudflared...", file=sys.stderr, flush=True)
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def main() -> None:
    """Main loop: watch for token changes and manage cloudflared lifecycle."""
    print(
        "[cloudflare-tunnel] Starting tunnel runner, watching runtime/secrets",
        file=sys.stderr,
        flush=True,
    )

    SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True)

    inotify_fd = _try_setup_inotify(SECRETS_FILE)
    if inotify_fd is not None:
        print(
            "[cloudflare-tunnel] Using inotify for file watching",
            file=sys.stderr,
            flush=True,
        )
    else:
        print(
            "[cloudflare-tunnel] inotify not available, using polling only",
            file=sys.stderr,
            flush=True,
        )

    current_token: str | None = None
    process: subprocess.Popen[bytes] | None = None
    last_mtime: float = 0.0

    def _handle_signal(signum: int, frame: object) -> None:
        _stop_cloudflared(process)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    while True:
        # Check for token changes
        try:
            new_mtime = SECRETS_FILE.stat().st_mtime if SECRETS_FILE.exists() else 0.0
        except OSError:
            new_mtime = 0.0

        if new_mtime != last_mtime:
            last_mtime = new_mtime
            new_token = _read_token(SECRETS_FILE)

            if new_token != current_token:
                if new_token is not None:
                    _stop_cloudflared(process)
                    process = _run_cloudflared(new_token)
                    current_token = new_token
                elif current_token is not None:
                    print(
                        "[cloudflare-tunnel] Token removed, stopping cloudflared",
                        file=sys.stderr,
                        flush=True,
                    )
                    _stop_cloudflared(process)
                    process = None
                    current_token = None

        # Check if cloudflared died unexpectedly
        if process is not None and process.poll() is not None:
            exit_code = process.returncode
            print(
                f"[cloudflare-tunnel] cloudflared exited with code {exit_code}, will restart on next check",
                file=sys.stderr,
                flush=True,
            )
            process = None
            # Force re-read on next iteration
            last_mtime = 0.0

        # Wait for changes via inotify (with polling fallback)
        if inotify_fd is not None:
            _wait_for_change_inotify(inotify_fd, POLL_INTERVAL_SECONDS)
        else:
            time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
