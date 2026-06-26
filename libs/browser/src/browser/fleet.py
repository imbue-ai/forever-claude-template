"""``agentic-browser-fleet``: drive the shared browser fleet from an agent's shell.

This is the CLI the Claude Code agent (the orchestrator) calls. It is a thin,
stateless HTTP client to the per-workspace browser daemon (runner.py). It does
NOT drive the browser itself: ``task`` hands a goal to a *browser-use* agent on
the chosen browser and streams that agent's Thinking/Action trace back here, to
the orchestrator's own output. ``take control`` is a human action in the UI, not
something this CLI does.

Ownership rules (enforced by the daemon, surfaced here):

* Each browser is controlled by exactly one party. ``task``/``lock`` acquire it;
  they release automatically when the command ends (the connection is the lease).
* Agents never preempt each other: ``task`` on a browser another agent holds
  waits in a FIFO queue until it is free (``--no-wait`` fails fast instead).
* A browser a human took control of is locked to the human. Resume only when the
  human tells you to ("keep going") -- then, and only then, pass ``--reclaim``.

Browsers are addressed by NAME (a random ~2-word english name like ``alex-smith``),
not a number. There is no default browser: run ``new`` first (it prints a name),
then drive that browser by its name.

Commands::

    agentic-browser-fleet ls
    agentic-browser-fleet new [name]
    agentic-browser-fleet task <name> "<prompt>" [--reclaim] [--no-wait] [--max-wait S] [--no-pane]
    agentic-browser-fleet lock <name> [--no-wait] [--max-wait S]    # foreground hold; Ctrl-C releases
    agentic-browser-fleet unlock <name>                             # alias: release
    agentic-browser-fleet release <name>

The daemon address is discovered from ``runtime/applications.toml`` (the same
registry ``layout.py`` reads), overridable via ``MINDS_BROWSER_SERVICE_URL``,
falling back to ``http://127.0.0.1:8081``. Browser panes are pulled into the
agent's view via ``scripts/layout.py`` (anchored at ``$BROWSER_FLEET_ANCHOR`` if
set -- a parent passes its chat ref to sub-agents -- else the caller's own chat).
"""

import argparse
import json
import os
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from typing import Iterator

from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.cli.output_helpers import write_stderr_line

_DEFAULT_URL = "http://127.0.0.1:8081"
_ENV_URL = "MINDS_BROWSER_SERVICE_URL"
_ENV_ANCHOR = "BROWSER_FLEET_ANCHOR"
_APPLICATIONS_FILE = "runtime/applications.toml"

# Exit codes the orchestrating agent can branch on.
_EXIT_OK = 0
_EXIT_ERROR = 1
_EXIT_PREEMPTED = 2  # a human took control mid-task
_EXIT_BUSY = 3  # held by a human (or another agent with --no-wait)
_EXIT_TIMEOUT = 4  # waited --max-wait and another agent still held it
_EXIT_USAGE = 64
_EXIT_NO_DAEMON = 69


def _out(message: str) -> None:
    write_human_line(message)


def _err(message: str) -> None:
    write_stderr_line(message)


def _repo_root() -> Path:
    """Walk up from cwd to the workspace root (where ``scripts/layout.py`` lives)."""
    here = Path.cwd()
    for candidate in (here, *here.parents):
        if (candidate / "scripts" / "layout.py").exists():
            return candidate
    return here


def _daemon_url() -> str:
    """Discover the browser daemon's base URL (env override, registry, then localhost)."""
    override = os.environ.get(_ENV_URL)
    if override:
        return override.rstrip("/")
    registry = Path(os.environ.get("MINDS_APPLICATIONS_FILE", _APPLICATIONS_FILE))
    if not registry.is_absolute():
        registry = _repo_root() / registry
    try:
        doc = tomllib.loads(registry.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return _DEFAULT_URL
    for app in doc.get("applications", []):
        if app.get("name") == "browser" and app.get("url"):
            return str(app["url"]).rstrip("/")
    return _DEFAULT_URL


def _agent_headers() -> dict[str, str]:
    """Identity headers; hard-fail if ``MNGR_AGENT_ID`` is unset (no null owner)."""
    agent_id = os.environ.get("MNGR_AGENT_ID")
    if not agent_id:
        _err("MNGR_AGENT_ID is not set -- run agentic-browser-fleet from inside an agent.")
        raise SystemExit(_EXIT_USAGE)
    headers = {"X-Mngr-Agent-Id": agent_id, "Content-Type": "application/json"}
    name = os.environ.get("MNGR_AGENT_NAME")
    if name:
        headers["X-Mngr-Agent-Name"] = name
    return headers


def _request(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    """Single JSON request/response. Returns ``(status_code, parsed_body)``."""
    url = _daemon_url() + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=_agent_headers())
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except json.JSONDecodeError:
            return e.code, {"error": e.reason}
    except urllib.error.URLError as e:
        _err(f"cannot reach the browser daemon at {_daemon_url()} ({e.reason}). Is it running?")
        raise SystemExit(_EXIT_NO_DAEMON) from e


def _stream(path: str, body: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """POST and yield each line of the NDJSON response as it arrives.

    Closing the iterator (or a KeyboardInterrupt) closes the connection, which the
    daemon sees as a disconnect and releases the browser -- the connection is the lease.
    """
    url = _daemon_url() + path
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers=_agent_headers())
    try:
        resp = urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or b"{}")
        except json.JSONDecodeError:
            payload = {"error": e.reason}
        yield {"type": "error", "text": payload.get("error", e.reason)}
        return
    except urllib.error.URLError as e:
        _err(f"cannot reach the browser daemon at {_daemon_url()} ({e.reason}). Is it running?")
        raise SystemExit(_EXIT_NO_DAEMON) from e
    with resp:
        for raw in resp:
            line = raw.decode().strip()
            if line:
                yield json.loads(line)


# --- pane pull-in (reuse scripts/layout.py) ----------------------------------


def _layout(*args: str, quiet: bool = False) -> bool:
    """Run ``scripts/layout.py`` with the given args from the repo root. True on success.
    ``quiet`` suppresses layout.py's raw stderr so the caller can substitute its own
    message (used by the pane-pull, which has a friendlier failure note)."""
    root = _repo_root()
    layout = root / "scripts" / "layout.py"
    if not layout.exists():
        return False
    result = subprocess.run(
        [sys.executable, str(layout), *args], cwd=str(root), capture_output=True, text=True
    )
    if result.returncode != 0 and not quiet:
        _err(result.stderr.strip() or f"layout {' '.join(args)} failed")
    return result.returncode == 0


def _pull_in_pane(browser_name: str) -> None:
    """Surface browser ``browser_name`` as its OWN pane to the right of the requesting
    agent's chat (each browser in a separate pane).

    ``--new-group`` forces a fresh pane rather than tabbing the browser into an
    existing pane group, so opening a second browser lands beside the first, not as a
    tab inside it. Splitting an already-open browser is a no-op that just focuses it,
    so this is safe to call repeatedly.

    Any agent the user started -- the primary, or one opened via "+ New agent" --
    surfaces the pane next to its OWN chat (``--relative-to self``). A launch-task /
    background agent has no chat in this workspace's UI (and may be a separate
    container), so the split can't land; we then say so in one clear line rather than
    leaking layout.py's raw 5s "service not registered" error. Either way the browser
    is running and reachable -- the pane is just a viewing convenience.
    """
    ref = f"service:browser?session={browser_name}"
    # A parent may hand a sub-agent its chat as an anchor; otherwise anchor on our own.
    anchor = os.environ.get(_ENV_ANCHOR)
    if anchor and _layout("split", ref, "--relative-to", anchor, "--direction", "right", "--new-group", quiet=True):
        return
    if _layout("split", ref, "--relative-to", "self", "--direction", "right", "--new-group", quiet=True):
        return
    _err(f"browser {browser_name} is running, but I couldn't open its live pane here. "
         'If you are the workspace\'s main agent, open it from the "+" menu; a background '
         "or sub-agent can't show panes (have the main agent drive the browser).")


# --- commands -----------------------------------------------------------------


def _owner_label(browser: dict[str, Any], me: str | None) -> str:
    if browser.get("crashed"):
        return "crashed (gone -- start a new one)"
    if browser["controller"] == "agent":
        name = browser.get("owner_name") or browser.get("owner_agent_id") or "?"
        return "you" if browser.get("owner_agent_id") == me else f"agent {name}"
    return "human (took control)" if browser.get("human_pinned") else "free"


def cmd_ls(args: argparse.Namespace) -> int:
    status, payload = _request("GET", "/browsers")
    if status != 200:
        _err(payload.get("error", f"ls failed ({status})"))
        return _EXIT_ERROR
    browsers = payload.get("browsers", [])
    if not browsers:
        _out("no browsers yet -- use `new` to start one (it prints a name to drive by)")
        return _EXIT_OK
    me = os.environ.get("MNGR_AGENT_ID")
    for browser in browsers:
        tabs = browser.get("tabs", [])
        active = next((t for t in tabs if t.get("active")), None)
        where = (active.get("url") or active.get("title") or "") if active else "(no tab)"
        waiting = browser.get("waiting") or []
        queued = f"  [queued: {', '.join(waiting)}]" if waiting else ""
        _out(f"browser {browser['id']}: {_owner_label(browser, me)} -- {len(tabs)} tab(s), active: {where}{queued}")
        if getattr(args, "include_tabs", False):
            for tab in tabs:
                mark = "*" if tab.get("active") else " "
                _out(f"    [{tab.get('index')}]{mark} {tab.get('title') or ''}  {tab.get('url', '')}")
    return _EXIT_OK


def cmd_new(args: argparse.Namespace) -> int:
    # `new` picks a random name; pass `new <name>` to choose one. A duplicate or invalid
    # name is rejected by the daemon (409 / 400) with a clear message.
    body: dict[str, Any] = {"name": args.name} if args.name else {}
    status, payload = _request("POST", "/browsers", body)
    if status == 200:
        # Surface the new browser's pane right away, so "open a new browser" visibly
        # opens one (idempotent with the pane-pull the first direct command also does).
        _pull_in_pane(payload["name"])
        _out(f"started browser {payload['name']}")
        return _EXIT_OK
    _err(payload.get("error", f"new failed ({status})"))
    if status == 400:  # invalid name -> a usage problem the agent can fix by picking another.
        return _EXIT_USAGE
    # 409 = duplicate name / fleet full, 503 = chromium installing -- both "try later".
    return _EXIT_BUSY if status in (409, 503) else _EXIT_ERROR


def cmd_close(args: argparse.Namespace) -> int:
    """Close an entire browser (all its tabs) and free its resources -- not just one tab.
    Use this when you're permanently done with a browser; the name is retired (never reused)."""
    status, payload = _request("DELETE", f"/browsers/{args.name}")
    if status != 200:
        _err(payload.get("error", f"close failed ({status})"))
        return _EXIT_BUSY if status == 503 else _EXIT_ERROR  # 503 = fleet still restoring
    _out(f"closed browser {args.name}")
    return _EXIT_OK


def _render_event(event: dict[str, Any], browser_name: str) -> int | None:
    """Print one task/hold event; return an exit code for terminal events, else None."""
    kind = event.get("type")
    if kind == "waiting":
        busy = event.get("busy_name") or event.get("busy_agent_id") or "another agent"
        _out(f"browser {browser_name} is busy ({busy}) -- waiting for it to free up...")
    elif kind == "acquired":
        _out(f"(working on browser {browser_name})")
    elif kind == "held":
        _out(f"holding browser {browser_name} (Ctrl-C to release)")
    elif kind == "thinking":
        _out(f"[thinking] {event.get('text', '')}")
    elif kind == "action":
        _out(f"[action] {event.get('text', '')}")
    elif kind == "done":
        _out(f"done: {event.get('result', '')}")
        return _EXIT_OK
    elif kind == "error":
        _err(f"error: {event.get('text', '')}")
        return _EXIT_ERROR
    elif kind == "preempted":
        _out(
            f"lost control of browser {browser_name} (you took over). "
            'Send me a message ("keep going", "resume", whatever) when you want me to continue.'
        )
        return _EXIT_PREEMPTED
    elif kind == "busy_human":
        _err(
            f"browser {browser_name} is under human control. It is yours to drive; when you are done, "
            'click "Return to agents" (or tell me to resume and I will reclaim it).'
        )
        return _EXIT_BUSY
    elif kind == "busy_agent":
        _err(f"browser {browser_name} is held by another agent (use without --no-wait to queue for it).")
        return _EXIT_BUSY
    elif kind == "timed_out":
        _err(f"browser {browser_name} is still held by another agent after waiting; gave up.")
        return _EXIT_TIMEOUT
    return None


def cmd_task(args: argparse.Namespace) -> int:
    if not args.no_pane:
        _pull_in_pane(args.name)
    body: dict[str, Any] = {"prompt": args.prompt, "reclaim": args.reclaim, "wait": not args.no_wait}
    if args.max_wait is not None:
        body["max_wait"] = args.max_wait
    exit_code = _EXIT_ERROR
    try:
        for event in _stream(f"/browsers/{args.name}/task", body):
            code = _render_event(event, args.name)
            if code is not None:
                exit_code = code
    except KeyboardInterrupt:
        _err("interrupted -- released the browser.")
        return _EXIT_OK
    return exit_code


def cmd_lock(args: argparse.Namespace) -> int:
    if not args.no_pane:
        _pull_in_pane(args.name)
    body: dict[str, Any] = {"wait": not args.no_wait}
    if args.max_wait is not None:
        body["max_wait"] = args.max_wait
    try:
        for event in _stream(f"/browsers/{args.name}/hold", body):
            code = _render_event(event, args.name)
            if code is not None and event.get("type") != "held":
                return code
    except KeyboardInterrupt:
        _err("released the browser.")
        return _EXIT_OK
    return _EXIT_OK


def cmd_release(args: argparse.Namespace) -> int:
    status, payload = _request("POST", f"/browsers/{args.name}/release")
    if status != 200:
        _err(payload.get("error", f"release failed ({status})"))
        return _EXIT_ERROR
    _out(f"released browser {args.name}" if payload.get("released") else f"browser {args.name} was not yours to release")
    return _EXIT_OK


# --- direct control: you drive the browser yourself, one command at a time ----


def _render_action(payload: dict[str, Any], browser_name: str, kind: str) -> int:
    """Print one direct-command result and return the exit code (owner-aware)."""
    if payload.get("ok"):
        if kind == "state":
            _out(f"browser {browser_name} @ {payload.get('url', '')}  ({payload.get('title', '')})")
            tabs = payload.get("tabs", [])
            if len(tabs) > 1:
                _out("tabs: " + ", ".join(f"[{t['index']}{'*' if t.get('active') else ''}] {t.get('url', '')}" for t in tabs))
            _out(payload.get("elements") or "(no interactive elements -- try screenshot)")
        elif kind == "screenshot":
            _out(f"screenshot saved: {payload.get('screenshot_path')}  (Read it to view)")
        elif kind == "tab":
            tabs = payload.get("tabs", [])
            _out("tabs: " + ", ".join(f"[{t['index']}{'*' if t.get('active') else ''}] {t.get('title') or t.get('url', '')}" for t in tabs))
        else:
            _out(f"ok: {kind}")
        return _EXIT_OK
    status = payload.get("status")
    if status == "busy_human":
        _out(f"browser {browser_name}: the human took control -- you're queued to resume. "
             "They can see you're waiting, and you'll be messaged to pick up when they hand it "
             f"back. Tell the user, then end your turn; re-run `state {browser_name}` when you resume.")
        return _EXIT_PREEMPTED
    if status == "busy_agent":
        _out(f"browser {browser_name} is held by another agent -- you're queued for it and will be "
             f"messaged when it frees. For unrelated work, use a different browser (or `new`); "
             f"re-run `state {browser_name}` when you resume.")
        return _EXIT_BUSY
    if status == "lost_control":
        _out(f"browser {browser_name}: the human took control mid-step -- you're queued to resume. "
             "Tell the user you'll pick up when they hand it back, then end your turn; "
             f"re-run `state {browser_name}` when you resume.")
        return _EXIT_PREEMPTED
    if status == "initializing":
        _err("the browser fleet is still starting up (restoring your saved browsers) -- "
             "try again in a few seconds. `ls` and `state` work now; this command needs the fleet ready.")
        return _EXIT_BUSY
    if status == "crashed":
        _err(f"browser {browser_name} crashed (Chromium was killed -- e.g. out of memory) and is gone. "
             f"Start a fresh one with `new` (it gets a new name); browser {browser_name} won't come back.")
        return _EXIT_ERROR
    if status == "stale_index":
        _err(payload.get("error") or f"that element index is stale -- run `state {browser_name}` again first")
        return _EXIT_ERROR
    if status == "timed_out":
        _err(f"browser {browser_name} stayed busy; gave up.")
        return _EXIT_TIMEOUT
    _err(payload.get("error") or f"command failed ({status})")
    return _EXIT_ERROR


def _action(browser_name: str, verb: str, kind: str, body: dict[str, Any] | None = None) -> int:
    status, payload = _request("POST", f"/browsers/{browser_name}/{verb}", body or {})
    if status == 404:
        _err(payload.get("error", f"no browser {browser_name}"))
        return _EXIT_ERROR
    # The first command for a browser (and the first after a human hands it back)
    # surfaces it as a pane split next to your chat, so the human can watch.
    if payload.get("newly_acquired"):
        _pull_in_pane(browser_name)
    return _render_action(payload, browser_name, kind)


def cmd_state(args: argparse.Namespace) -> int:
    return _action(args.name, "state", "state")


def cmd_open(args: argparse.Namespace) -> int:
    return _action(args.name, "navigate", "navigate", {"url": args.url})


def cmd_click(args: argparse.Namespace) -> int:
    return _action(args.name, "click", "click", {"index": args.index})


def cmd_input(args: argparse.Namespace) -> int:
    return _action(args.name, "input", "input", {"index": args.index, "text": args.text})


def cmd_select(args: argparse.Namespace) -> int:
    return _action(args.name, "select", "select", {"index": args.index, "value": args.value})


def cmd_scroll(args: argparse.Namespace) -> int:
    return _action(args.name, "scroll", "scroll", {"direction": args.direction, "amount": args.amount})


def cmd_keys(args: argparse.Namespace) -> int:
    return _action(args.name, "keys", "keys", {"keys": args.keys})


def cmd_screenshot(args: argparse.Namespace) -> int:
    return _action(args.name, "screenshot", "screenshot")


def cmd_tab(args: argparse.Namespace) -> int:
    body: dict[str, Any] = {"action": args.action}
    if args.index is not None:
        body["index"] = args.index
    if args.url is not None:
        body["url"] = args.url
    return _action(args.name, "tab", "tab", body)


def cmd_acquire(args: argparse.Namespace) -> int:
    status, payload = _request("POST", f"/browsers/{args.name}/acquire", {"reclaim": args.reclaim})
    if payload.get("ok"):
        _pull_in_pane(args.name)
        _out(f"acquired browser {args.name}")
        return _EXIT_OK
    return _render_action(payload, args.name, "acquire")


def cmd_handoff(args: argparse.Namespace) -> int:
    """Hand a browser to the human for a CAPTCHA / robot-check / login you can't do."""
    status, payload = _request("POST", f"/browsers/{args.name}/handoff", {"reason": args.reason})
    if status == 404:
        _err(payload.get("error", f"no browser {args.name}"))
        return _EXIT_ERROR
    if payload.get("ok"):
        _pull_in_pane(args.name)  # surface/focus the pane so the human sees what to solve
        _out(
            f"handed browser {args.name} to the human: {args.reason}. You're first in line to "
            f"resume. Tell the user what to do, end your turn, and re-run `state {args.name}` "
            "when they hand control back."
        )
        return _EXIT_PREEMPTED
    if payload.get("status") == "not_owner":
        _out(
            f"browser {args.name} isn't yours to hand off -- a human may already control it. "
            f"Run `state {args.name}` to see who has it; if the human took over, you're queued to resume."
        )
        return _EXIT_PREEMPTED
    return _render_action(payload, args.name, "handoff")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentic-browser-fleet", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_ls = sub.add_parser("ls", help="List active browsers, their owners, and their tabs.")
    p_ls.add_argument("--include-tabs", action="store_true", help="List every open tab per browser, not just the active one.")
    p_ls.set_defaults(func=cmd_ls)
    p_new = sub.add_parser("new", help="Start a new browser and print its name. Pass an optional name to choose one.")
    p_new.add_argument("name", nargs="?", default=None, help="Optional name (lowercase letters/digits/dashes, e.g. 'alex-smith'); a duplicate is rejected.")
    p_new.set_defaults(func=cmd_new)

    p_close = sub.add_parser("close", help="Close an entire browser (all tabs) and retire its name. For one tab, use `tab <name> close`.")
    p_close.add_argument("name")
    p_close.set_defaults(func=cmd_close)

    p_task = sub.add_parser("task", help="Run a browser-use task on a browser; stream its trace.")
    p_task.add_argument("name", help="Browser name (from `ls` or the name `new` printed).")
    p_task.add_argument("prompt", help="The high-level goal for the browser-use agent.")
    p_task.add_argument("--reclaim", action="store_true", help="Resume a browser a human took control of -- ONLY when the human told you to.")
    p_task.add_argument("--no-wait", action="store_true", help="Fail fast if another agent holds it, instead of queueing.")
    p_task.add_argument("--max-wait", type=float, default=None, help="Seconds to wait for another agent to release before giving up.")
    p_task.add_argument("--no-pane", action="store_true", help="Do not pull the browser into a UI pane.")
    p_task.set_defaults(func=cmd_task)

    p_lock = sub.add_parser("lock", help="Hold a browser (foreground) until Ctrl-C; releases on exit.")
    p_lock.add_argument("name")
    p_lock.add_argument("--no-wait", action="store_true")
    p_lock.add_argument("--max-wait", type=float, default=None)
    p_lock.add_argument("--no-pane", action="store_true")
    p_lock.set_defaults(func=cmd_lock)

    for verb in ("unlock", "release"):
        p_rel = sub.add_parser(verb, help="Release a browser you hold.")
        p_rel.add_argument("name")
        p_rel.set_defaults(func=cmd_release)

    # --- direct control: YOU drive. Run `state` to see numbered elements, then click. ---
    p_state = sub.add_parser("state", help="Show the page: numbered clickable elements + url + tabs. Run this before clicking.")
    p_state.add_argument("name")
    p_state.set_defaults(func=cmd_state)

    p_open = sub.add_parser("open", help="Navigate a browser to a URL.")
    p_open.add_argument("name")
    p_open.add_argument("url")
    p_open.set_defaults(func=cmd_open)

    p_click = sub.add_parser("click", help="Click the element with the given index (from `state`).")
    p_click.add_argument("name")
    p_click.add_argument("index", type=int)
    p_click.set_defaults(func=cmd_click)

    p_input = sub.add_parser("input", help="Type text into the element with the given index.")
    p_input.add_argument("name")
    p_input.add_argument("index", type=int)
    p_input.add_argument("text")
    p_input.set_defaults(func=cmd_input)

    p_select = sub.add_parser("select", help="Choose an option in a <select> dropdown by visible text.")
    p_select.add_argument("name")
    p_select.add_argument("index", type=int)
    p_select.add_argument("value")
    p_select.set_defaults(func=cmd_select)

    p_scroll = sub.add_parser("scroll", help="Scroll the page (down/up).")
    p_scroll.add_argument("name")
    p_scroll.add_argument("direction", nargs="?", default="down", choices=["down", "up"])
    p_scroll.add_argument("--amount", type=int, default=500, help="Pixels to scroll.")
    p_scroll.set_defaults(func=cmd_scroll)

    p_keys = sub.add_parser("keys", help='Send keyboard keys (e.g. "Enter", "Control+a").')
    p_keys.add_argument("name")
    p_keys.add_argument("keys")
    p_keys.set_defaults(func=cmd_keys)

    p_shot = sub.add_parser("screenshot", help="Save a PNG of the browser and print its path (Read it to view).")
    p_shot.add_argument("name")
    p_shot.set_defaults(func=cmd_screenshot)

    p_tab = sub.add_parser("tab", help="Tabs within a browser: list / switch / new / close.")
    p_tab.add_argument("name")
    p_tab.add_argument("action", nargs="?", default="list", choices=["list", "switch", "new", "close"])
    p_tab.add_argument("index", type=int, nargs="?", default=None, help="Tab index for switch/close.")
    p_tab.add_argument("--url", default=None, help="URL for `tab new`.")
    p_tab.set_defaults(func=cmd_tab)

    p_acq = sub.add_parser("acquire", help="Reserve a browser across several commands (optional; the first command auto-acquires).")
    p_acq.add_argument("name")
    p_acq.add_argument("--reclaim", action="store_true", help="Take a browser back from a human -- ONLY when they told you to resume.")
    p_acq.set_defaults(func=cmd_acquire)

    for verb in ("handoff", "request-human"):
        p_handoff = sub.add_parser(
            verb,
            help="Hand a browser to the human for a CAPTCHA / robot-check / login you can't do, then stop.",
        )
        p_handoff.add_argument("name")
        p_handoff.add_argument("reason", nargs="?", default="human verification needed", help="What the human needs to do (e.g. 'solve the CAPTCHA').")
        p_handoff.set_defaults(func=cmd_handoff)

    return parser


def main() -> None:
    args = _build_parser().parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
