"""Diagnostic watchdog: dump all thread stacks when the server stops answering.

Temporary instrumentation to root-cause an intermittent wedge where the
``system_interface`` process stays alive but stops accepting on its port
(``curl :PORT -> 000``). A daemon thread self-probes the server every
``_PROBE_INTERVAL_S`` seconds; after ``_FAILURES_BEFORE_DUMP`` consecutive
probe failures it writes ``faulthandler`` all-thread tracebacks (so we see
exactly which thread is stuck on what) to ``_DUMP_PATH`` and stderr.

Any HTTP response -- including 404 -- counts as alive; only a connection
refusal/timeout counts as a failure. Self-contained (stdlib only) and wrapped
so it can never crash the server.
"""

import faulthandler
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

_PROBE_INTERVAL_S = 5.0
_PROBE_TIMEOUT_S = 3.0
_FAILURES_BEFORE_DUMP = 3
_MAX_DUMPS = 6
_DUMP_PATH = Path("/tmp/system_interface_hang_dump.txt")


def _probe_once(host: str, port: int) -> bool:
    """Return True if the server answered (any HTTP status), False if unreachable."""
    url = f"http://{host}:{port}/__hang_watchdog_probe"
    try:
        urllib.request.urlopen(url, timeout=_PROBE_TIMEOUT_S).close()
        return True
    except urllib.error.HTTPError:
        # Server responded (e.g. 404) -- it's alive and accepting.
        return True
    except (urllib.error.URLError, socket.timeout, OSError):
        return False


def _watchdog_loop(host: str, port: int) -> None:
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    consecutive_failures = 0
    dumps_written = 0
    while dumps_written < _MAX_DUMPS:
        time.sleep(_PROBE_INTERVAL_S)
        if _probe_once(probe_host, port):
            consecutive_failures = 0
            continue
        consecutive_failures += 1
        if consecutive_failures < _FAILURES_BEFORE_DUMP:
            continue
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        header = f"\n===== HANG WATCHDOG: {probe_host}:{port} unreachable x{consecutive_failures} @ {stamp} =====\n"
        try:
            with _DUMP_PATH.open("a") as f:
                f.write(header)
                f.flush()
                faulthandler.dump_traceback(file=f, all_threads=True)
        except OSError:
            pass
        import sys

        sys.stderr.write(header)
        sys.stderr.flush()
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
        dumps_written += 1


def start_hang_watchdog(host: str, port: int) -> None:
    """Start the diagnostic watchdog daemon thread. Never raises."""
    try:
        thread = threading.Thread(
            target=_watchdog_loop,
            args=(host, port),
            name="hang-watchdog",
            daemon=True,
        )
        thread.start()
    except Exception:
        pass
